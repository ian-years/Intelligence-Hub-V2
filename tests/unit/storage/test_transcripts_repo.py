"""`transcripts` 表的 Repository 测试。

看护两条：
- **V1 §7.5**：`text_path` 统一，不再按平台不对称。V1 三份 `postprocess_*.py`
  各写各的目录、前端按平台分支读，改错一处就读不到口播稿。V2 里这条路径
  由 `FileStorage.transcript_path()` 一处决定，存储层只是存下来 ——
  所以这里的用例断言的是"**存什么读什么**，不做任何平台相关的加工"。
- **`video_id` 是主键 + `ON DELETE CASCADE`**：一条作品只有一份口播稿，
  重跑 ASR 是整行替换而不是插第二条；作品被删时口播稿行必须跟着走
  （磁盘上的 txt 不动，那是全库唯一的原始产物）。
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from intelligence_hub_v2.errors import NotFoundError, StorageError
from intelligence_hub_v2.models.transcript import TranscriptDraft
from intelligence_hub_v2.models.video import Video, VideoDraft
from intelligence_hub_v2.storage.db import SqliteStorage


def _draft(
    *,
    engine: str = "sherpa_sense_voice",
    text_path: str = "media/douyin/姜胡说/7642363455722229042-测试作品/transcript/speech-clean.txt",
    char_count: int = 872,
    sentence_count: int = 25,
    language: str | None = "zh",
    segments_json: str | None = None,
    content_summary: str | None = None,
    key_points: str | None = None,
    summary_method: str | None = None,
) -> TranscriptDraft:
    return TranscriptDraft(
        engine=engine,
        language=language,
        char_count=char_count,
        sentence_count=sentence_count,
        text_path=text_path,
        segments_json=segments_json,
        content_summary=content_summary,
        key_points=key_points,
        summary_method=summary_method,
    )


async def test_attach_then_get(storage: SqliteStorage, video_row: Video) -> None:
    record = await storage.transcripts.attach(video_row.id, _draft())
    assert record.video_id == video_row.id
    assert record.engine == "sherpa_sense_voice"
    assert record.char_count == 872

    fetched = await storage.transcripts.get_for_video(video_row.id)
    assert fetched is not None
    assert fetched.text_path == record.text_path
    assert fetched.language == "zh"


async def test_get_for_missing_video_returns_none(storage: SqliteStorage) -> None:
    assert await storage.transcripts.get_for_video(999_999) is None


async def test_get_or_raise_on_missing(storage: SqliteStorage) -> None:
    with pytest.raises(NotFoundError, match="transcript"):
        await storage.transcripts.get_or_raise(999_999)


async def test_attach_requires_the_video_to_exist(storage: SqliteStorage) -> None:
    """外键是真的：给一个不存在的 video_id 必须红，不能静默插进去。"""
    with pytest.raises(StorageError):
        await storage.transcripts.attach(999_999, _draft())


async def test_reattach_replaces_rather_than_duplicates(
    storage: SqliteStorage, video_row: Video
) -> None:
    """重跑 ASR 是正常操作。`video_id` 是主键，所以只能整行替换。"""
    await storage.transcripts.attach(video_row.id, _draft(char_count=100, sentence_count=3))
    second = await storage.transcripts.attach(
        video_row.id, _draft(char_count=999, sentence_count=40)
    )

    assert second.char_count == 999
    assert second.sentence_count == 40
    assert await storage.transcripts.count() == 1


async def test_reattach_clears_stale_columns(storage: SqliteStorage, video_row: Video) -> None:
    """替换是**整行**替换：上一轮的 `language` / `segments_json` / 摘要三列都不能残留。

    残留会造出"引擎换了、语言还是旧的"这种半新半旧的行 ——
    前端按 language 挑渲染分支时会走错。摘要那三列同族，而且更贵：
    **库里留着一份指向另一份稿子的摘要**是 ADR-0015 不放 `videos` 的全部理由，
    这里把它钉成行为而不是靠 handler 记得清。
    """
    await storage.transcripts.attach(
        video_row.id,
        _draft(
            language="zh",
            segments_json='[{"start_seconds": 0}]',
            content_summary="上一份稿子的摘要",
            key_points="- 上一份稿子的要点",
            summary_method="v1-imported",
        ),
    )
    replaced = await storage.transcripts.attach(
        video_row.id, _draft(language=None, segments_json=None)
    )

    assert replaced.language is None
    assert replaced.segments_json is None
    assert (replaced.content_summary, replaced.key_points, replaced.summary_method) == (
        None,
        None,
        None,
    )


async def test_segments_json_roundtrips_unicode(storage: SqliteStorage, video_row: Video) -> None:
    """口播稿片段是中文。`ensure_ascii=False` 的判据：库里存的是人能读的文本。"""
    segments = [{"start_seconds": 0.0, "end_seconds": 3.2, "text": "大家好我是姜胡说"}]
    await storage.transcripts.attach(
        video_row.id, _draft(segments_json=json.dumps(segments, ensure_ascii=False))
    )
    fetched = await storage.transcripts.get_or_raise(video_row.id)
    assert fetched.segments_json is not None
    assert "姜胡说" in fetched.segments_json
    assert json.loads(fetched.segments_json) == segments


async def test_delete_removes_row_only(storage: SqliteStorage, video_row: Video) -> None:
    await storage.transcripts.attach(video_row.id, _draft())
    assert await storage.transcripts.delete(video_row.id) is True
    assert await storage.transcripts.get_for_video(video_row.id) is None
    # 幂等
    assert await storage.transcripts.delete(video_row.id) is False


async def test_delete_video_cascades_to_transcript(
    storage: SqliteStorage, video_row: Video
) -> None:
    """`ON DELETE CASCADE`：作品行没了，口播稿行必须跟着没。

    留着孤儿行会让 `list_missing()` 误判"这条已经有口播稿了"，
    于是这条作品永远不会被重新转写 —— 而且没人看得出来。
    """
    await storage.transcripts.attach(video_row.id, _draft())
    assert await storage.transcripts.count() == 1

    await storage.videos.delete(video_row.id)

    assert await storage.transcripts.count() == 0
    assert await storage.transcripts.get_for_video(video_row.id) is None


async def test_list_missing_skips_videos_that_have_transcripts(
    storage: SqliteStorage, video_row: Video, make_video_draft
) -> None:
    other = await storage.videos.insert(make_video_draft("7642363455722229043"))
    await storage.transcripts.attach(video_row.id, _draft())

    missing = await storage.transcripts.list_missing()
    assert missing == [other.id]


async def test_list_missing_skips_hidden_videos(storage: SqliteStorage, video_row: Video) -> None:
    """V1 §7.25 的延伸：隐藏的作品不该被后处理任务捞起来转写。

    人会隐藏一条作品正是因为它不想要 —— 再去给它跑一遍 ASR
    是白花几十秒 CPU，而且转写完它也不会出现在列表里。
    """
    await storage.videos.hide(video_row.id, reason="不要这条")
    assert await storage.transcripts.list_missing() == []

    await storage.videos.unhide(video_row.id)
    assert await storage.transcripts.list_missing() == [video_row.id]


async def test_list_missing_filters_by_platform(
    storage: SqliteStorage, video_row: Video, make_video_draft
) -> None:
    await storage.platforms.upsert("bilibili", enabled=True)
    bili = await storage.videos.insert(make_video_draft("BV1cSec6tEux", platform="bilibili"))

    assert await storage.transcripts.list_missing(platform="douyin") == [video_row.id]
    assert await storage.transcripts.list_missing(platform="bilibili") == [bili.id]


async def test_list_missing_respects_limit(
    storage: SqliteStorage, video_row: Video, make_video_draft
) -> None:
    for i in range(5):
        await storage.videos.insert(make_video_draft(f"v{i}"))
    assert len(await storage.transcripts.list_missing(limit=2)) == 2


async def test_count_filters_by_platform(
    storage: SqliteStorage, video_row: Video, make_video_draft
) -> None:
    await storage.platforms.upsert("bilibili", enabled=True)
    bili = await storage.videos.insert(make_video_draft("BV1cSec6tEux", platform="bilibili"))

    await storage.transcripts.attach(video_row.id, _draft())
    await storage.transcripts.attach(
        bili.id,
        _draft(
            engine="bilibili_subtitle", text_path="media/bilibili/x/transcript/speech-clean.txt"
        ),
    )

    assert await storage.transcripts.count() == 2
    assert await storage.transcripts.count(platform="douyin") == 1
    assert await storage.transcripts.count(platform="bilibili") == 1
    assert await storage.transcripts.count(platform="youtube") == 0


async def test_text_path_is_stored_verbatim(storage: SqliteStorage, video_row: Video) -> None:
    """**V1 §7.5 的看护点**：存储层对路径不做任何平台相关的加工。

    两个平台用同一个 `transcript/speech-clean.txt` 布局写进去，读出来必须逐字相同 ——
    V1 的毛病正是"抖音写在 A 目录、B站写在 B 目录"，前端按平台分支去读。
    """
    await storage.platforms.upsert("bilibili", enabled=True)
    bili = await storage.videos.insert(
        VideoDraft(
            platform="bilibili",
            platform_video_id="BV1cSec6tEux",
            title="B站作品",
            media_path="media/bilibili/某UP/BV1cSec6tEux-B站作品/video.mp4",
            media_source="dash_merged",
        )
    )

    douyin_path = "media/douyin/姜胡说/7642363455722229042-测试作品/transcript/speech-clean.txt"
    bili_path = "media/bilibili/某UP/BV1cSec6tEux-B站作品/transcript/speech-clean.txt"
    await storage.transcripts.attach(video_row.id, _draft(text_path=douyin_path))
    await storage.transcripts.attach(
        bili.id, _draft(text_path=bili_path, engine="bilibili_subtitle")
    )

    assert (await storage.transcripts.get_or_raise(video_row.id)).text_path == douyin_path
    assert (await storage.transcripts.get_or_raise(bili.id)).text_path == bili_path
    # 两条路径的**尾段布局**一致，这是"统一"的可测判据
    assert douyin_path.endswith("/transcript/speech-clean.txt")
    assert bili_path.endswith("/transcript/speech-clean.txt")


async def test_unknown_engine_is_rejected(storage: SqliteStorage, video_row: Video) -> None:
    """DB 的 CHECK 枚举。加引擎要同时改 schema 与 models.transcript.Transcript。"""
    with pytest.raises(StorageError):
        await storage.transcripts.attach(video_row.id, _draft(engine="whisper_large_v3"))


async def test_unknown_summary_method_is_rejected(storage: SqliteStorage, video_row: Video) -> None:
    """DB 那侧的 CHECK 枚举（ADR-0015）：V2.2 加生成式摘要时要同时改 schema 与迁移。"""
    with pytest.raises(StorageError):
        await storage.transcripts.attach(
            video_row.id, _draft(content_summary="摘要", summary_method="gpt-4o")
        )


def test_a_summary_without_provenance_is_refused() -> None:
    """Python 这侧的闸：有摘要却没写来源，草稿就组不出来。

    `summary_method` 不是元数据是凭据 —— 本地抽取式（≤600 字原文片段）与 V1 搬来的
    那份（实测最长 2982 字整篇改写）长得一模一样，不标来源就分不出该信哪条。
    """
    with pytest.raises(ValidationError, match="summary_method"):
        TranscriptDraft(
            engine="manual",
            char_count=1,
            sentence_count=1,
            text_path="media/douyin/x/1/transcript/speech-clean.txt",
            content_summary="一句摘要",
        )


async def test_negative_counts_are_rejected(storage: SqliteStorage, video_row: Video) -> None:
    with pytest.raises(StorageError):
        await storage.transcripts.attach(video_row.id, _draft(char_count=-1))
    with pytest.raises(StorageError):
        await storage.transcripts.attach(video_row.id, _draft(sentence_count=-1))
