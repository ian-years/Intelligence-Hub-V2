"""`topics` / `drafts` 两张表的 Repository 不变量（V2.2 T5.5，ADR-0021）。

四条判据，都写成**关系**而不是样本：

1. **部分更新不许抹掉别的列**（V1 §7.4，`update_fields()` 存在的全部理由）：
   只改一列，其余每一列逐条问一遍"你还是原值吗"。
2. **删除的语义**：删掉源作品之后草稿必须在、`source_video_id` 必须已经空掉，
   判据是"库里指向不存在作品的草稿条数 == 0"，且**删之前删之后各问一遍** ——
   只在事后成立的断言守不住"事前就已经不一致"。
3. **空白唯一键拒收**：空名选题在 Python 层就被拒（库里那一半是 UNIQUE，空串合法）。
4. **CHECK 真的在执行**：绕过 Pydantic 直接 UPDATE 一个枚举外的状态，库当场拒。

跑在 `storage` 这个双后端 fixture 上（内存 = `create_all`，文件 = 真迁移），
所以第 4 条同时验到"迁移里那段 CHECK 有没有落进库、并且真的管得住写入"。

**变异检查（本文件全绿之后逐条手工做过，2026-09-24，四条都真的会红）**：
- `DraftRepository.update_fields` 换成整行覆盖 →
  `test_update_fields_touches_only_the_columns_it_was_given` 红（**实测 2 failed**：
  内存档与文件档各一条）。
- `drafts.source_video_id` 的 FK 从 `SET NULL` 改成 `CASCADE` →
  `test_deleting_the_source_video_keeps_the_draft_and_clears_its_reference` 红
  （**实测 1 failed / 1 passed**：只有内存档红 —— 文件档的库是迁移 0004 建的，
  改 `schema.py` 改不到它。这正是"为什么两条链都要跑"的活样本，
  也是为什么 0004 的 `downgrade()` 必须真能删表）。
- 去掉 metadata 里的 `_enum_check("status", …)` →
  `test_the_db_check_rejects_a_status_the_python_layer_does_not_know` 红（**实测 1 failed**，
  同样只有内存档）。注意 `EXPECTED_CHECKS` 与 `check_schema_matches_migrations()`
  在这次变异下**都没红**：前者比的是"迁移建出来的库里有几条 CHECK"（迁移文本没动，
  约束还在），后者的 `compare_metadata` 看不见 CHECK。
  所以这一条用例不是冗余 —— 它管的是"metadata 那一侧还有没有这条约束"，
  而那恰好是另外两道闸都照不到的方向。
- 去掉 `TopicDraft.model_post_init` 的拒收 →
  `test_a_blank_topic_name_is_refused_before_it_can_occupy_the_unique_key` 红（**实测 2 failed**）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from intelligence_hub_v2.errors import ConflictError, NotFoundError, StorageError
from intelligence_hub_v2.models.draft import DraftInput
from intelligence_hub_v2.models.topic import Topic, TopicDraft
from intelligence_hub_v2.models.video import Video
from intelligence_hub_v2.storage.db import SqliteStorage
from intelligence_hub_v2.storage.schema import drafts_table, videos_table


async def _orphan_draft_count(storage: SqliteStorage) -> int:
    """**指向已经不存在的作品**的草稿条数。0 才叫没有孤儿。

    写成一条 SQL 而不是"把草稿的 id 收到 Python 里逐个查"：后者要拿两份清单比，
    而"某一份漏了一个"正是这类断言最常见的失效方式（V1 §7.12 的比对型判据同形）。
    """
    async with storage.sessionmaker() as session:
        stmt = (
            select(func.count())
            .select_from(drafts_table)
            .where(
                drafts_table.c.source_video_id.is_not(None),
                drafts_table.c.source_video_id.not_in(select(videos_table.c.id)),
            )
        )
        return int((await session.execute(stmt)).scalar_one())


@pytest.fixture
async def topic(storage: SqliteStorage) -> Topic:
    return await storage.topics.insert(TopicDraft(name="AI 做内容", description="第一批选题"))


@pytest.fixture
async def draft(video_row: Video) -> DraftInput:
    return DraftInput(title="AI 做内容的三条路", content="正文第一段", source_video_id=video_row.id)


# ---------------------------------------------------------------------------
# topics
# ---------------------------------------------------------------------------


async def test_insert_then_list_and_get_round_trip(storage: SqliteStorage) -> None:
    """前置防空转：起点是 0 条，插完是 1 条，`get` 与 `list_all` 给同一条。"""
    assert await storage.topics.count() == 0
    created = await storage.topics.insert(TopicDraft(name="选题甲"))
    assert await storage.topics.count() == 1
    assert await storage.topics.get(created.id) == created
    assert [t.id for t in await storage.topics.list_all()] == [created.id]


async def test_a_blank_topic_name_is_refused_before_it_can_occupy_the_unique_key(
    storage: SqliteStorage,
) -> None:
    """**空名在模型层就拒**，不给它进库占住唯一键的机会。

    防空转的前置：非空的名字能正常写入并留在库里，所以红的不是"什么都写不进去"，
    而是"这一条偏偏写进去了"。
    """
    ok = await storage.topics.insert(TopicDraft(name="能存"))
    assert ok.name == "能存"
    with pytest.raises(ValueError, match="为空"):
        TopicDraft(name="   ")
    assert await storage.topics.count() == 1, "拒收不许顺手多留一条空名选题"


async def test_names_are_stripped_so_two_spellings_do_not_become_two_topics(
    storage: SqliteStorage,
) -> None:
    """`"  AI 选题 "` 与 `"AI 选题"` 是同一条选题：不 strip 的话它们在 UNIQUE 里是两行，
    而用户在列表里看得出它们是同一个选题 —— 手工判重比这里拒一次贵。"""
    first = await storage.topics.insert(TopicDraft(name="  AI 选题  "))
    assert first.name == "AI 选题"
    with pytest.raises(ConflictError):
        await storage.topics.insert(TopicDraft(name="AI 选题"))
    assert await storage.topics.count() == 1


async def test_delete_removes_exactly_one_row_and_is_not_replayable(
    storage: SqliteStorage, topic: Topic
) -> None:
    assert await storage.topics.delete(topic.id) is True
    assert await storage.topics.count() == 0
    assert await storage.topics.get(topic.id) is None
    # 第二次删回 False（幂等判据在这一层），API 层据此给 404 而不是 204。
    assert await storage.topics.delete(topic.id) is False


async def test_update_fields_rejects_unknown_columns(storage: SqliteStorage, topic: Topic) -> None:
    """白名单是**运行期**的：`**dict` 展开与请求体字段名 mypy 看不见。"""
    with pytest.raises(TypeError, match="name"):
        await storage.topics.update_fields(topic.id, **{"name": "换掉了"})  # type: ignore[misc]
    assert (await storage.topics.get_or_raise(topic.id)).name == "AI 做内容"
    # 同一句里的正向半句：允许的那一列改得动。
    changed = await storage.topics.update_fields(topic.id, description="改过的描述")
    assert changed.description == "改过的描述"
    assert changed.name == "AI 做内容"


# ---------------------------------------------------------------------------
# drafts：部分更新（V1 §7.4 那一族）
# ---------------------------------------------------------------------------


async def test_update_fields_touches_only_the_columns_it_was_given(
    storage: SqliteStorage, draft: DraftInput, video_row: Video
) -> None:
    """**这一条是 `update_fields()` 存在的全部理由**。

    只改 `title`，然后逐列问：`content` / `source_video_id` / `status` / `created_at`
    一个字没动，只有 `updated_at` 往前走。整行覆盖的实现在这里会红 ——
    调用方手里根本没有那些列，覆盖回去的就是空值。
    """
    created = await storage.drafts.insert(draft)
    assert created.source_video_id == video_row.id
    before = created.model_dump()

    updated = await storage.drafts.update_fields(created.id, title="新标题")
    after = updated.model_dump()

    assert updated.title == "新标题"
    for column in ("id", "content", "source_video_id", "status", "created_at"):
        assert after[column] == before[column], f"{column} 被 update_fields(title=…) 改了"
    assert after["updated_at"] >= before["updated_at"]


async def test_empty_update_fields_is_not_a_silent_row_rewrite(
    storage: SqliteStorage, draft: DraftInput
) -> None:
    """一个字段都没传时**不发 SQL**：连 `updated_at` 都不许动。

    为什么单独一条：上一例改了一列，看不出"空参数被当成整行覆盖"错在哪
    （那种实现的 dict 是空的，覆盖出来的正好是当前行，别的列照样"没变"）。
    时间戳是这里唯一能证伪它的东西。
    """
    created = await storage.drafts.insert(draft)
    again = await storage.drafts.update_fields(created.id)
    assert again.updated_at == created.updated_at
    assert again.content == created.content


async def test_update_fields_on_a_missing_row_raises_not_found(storage: SqliteStorage) -> None:
    with pytest.raises(NotFoundError):
        await storage.drafts.update_fields(999_999, title="没有这一行")


async def test_the_db_check_rejects_a_status_the_python_layer_does_not_know(
    storage: SqliteStorage, draft: DraftInput
) -> None:
    """绕过 Pydantic 直接写库 → 库里那条 `ck_drafts_status_enum` 拒收。

    与 `EXPECTED_CHECKS` 的分工：那份清单管"约束**在不在**"，这一条管
    "在着的约束**真的执行**"。batch 模式重建表是 SQLite 上加约束的唯一方式，
    那条路径出岔子时约束会静默消失，而 `compare_metadata` 看不见 CHECK。
    """
    created = await storage.drafts.insert(draft)
    with pytest.raises(IntegrityError):
        async with storage.sessionmaker() as session, session.begin():
            await session.execute(
                update(drafts_table).where(drafts_table.c.id == created.id).values(status="drafted")
            )
    # 拒收之后原值还在（不是"写坏了再也没人管"）。
    assert (await storage.drafts.get_or_raise(created.id)).status == "draft"


# ---------------------------------------------------------------------------
# 删除的语义：外键
# ---------------------------------------------------------------------------


async def test_deleting_the_source_video_keeps_the_draft_and_clears_its_reference(
    storage: SqliteStorage, draft: DraftInput, video_row: Video
) -> None:
    """`ON DELETE SET NULL`：删作品**不销毁人写出来的字**。

    判据是关系不是样本：删前删后各问一遍"有没有草稿指向不存在的作品"，两边都是 0；
    再加"草稿条数一根毛没少"+"那一条的来源已经空了"+"正文还是那篇正文"。
    """
    created = await storage.drafts.insert(draft)
    assert await _orphan_draft_count(storage) == 0

    assert await storage.videos.delete(video_row.id) is True

    assert await _orphan_draft_count(storage) == 0
    assert await storage.drafts.count() == 1
    row = await storage.drafts.get_or_raise(created.id)
    assert row.source_video_id is None
    assert row.content == created.content, "SET NULL 顺手把正文改了，那是另一种整行覆盖"


async def test_an_unknown_source_video_never_reaches_the_table(storage: SqliteStorage) -> None:
    """外键在**写入侧**也成立：引用不存在的作品当场拒，不留一条断链草稿。"""
    with pytest.raises(StorageError, match="外键"):
        await storage.drafts.insert(DraftInput(title="断链", content="正文", source_video_id=4242))
    assert await storage.drafts.count() == 0
    assert await _orphan_draft_count(storage) == 0
