"""`CollectParams.metrics_only`：V1 `--collection-strategy 仅采集数据` 的 V2 形状（T6.8）。

T6.8 点名的三个参数里只有这一个落地，另两个的处置写在 `docs/progress/2026-09-25.md`
（读完 V1 `download_douyin_latest.py` 的结论：`--creator-source` 只进清单、不改取数路径；
`--cross-platform-identity` 全脚本没有任何一处读它）。

这里最要紧的一条不是"能跳过下载"，而是**跳过下载不许顺手改写已有行**：
一轮 `metrics_only` 撞上一批已经采过的作品，如果它走"没有媒体也插一条/覆盖一条"，
症状是媒体路径与稿子一起消失，而任务状态是绿的。
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.unit.tasks.conftest import (
    FakeAdapter,
    FakeBus,
    FakeConfig,
    FakeRegistry,
    make_ctx,
    make_video_meta,
)

from intelligence_hub_v2.models.creator import CreatorDraft
from intelligence_hub_v2.models.media import SingleFileArtifact
from intelligence_hub_v2.models.video import VideoMeta
from intelligence_hub_v2.tasks.collect import build_video_draft, make_collect_handler
from intelligence_hub_v2.tasks.definition import TaskContext
from intelligence_hub_v2.tasks.params import CollectParams

PLATFORM = "douyin"


async def _seed_creator(storage) -> int:
    creator = await storage.creators.insert(
        CreatorDraft(
            platform=PLATFORM,
            platform_id="c1",
            name="姜胡说",
            profile_url="https://douyin.com/user/c1",
            is_tracking=True,
        )
    )
    return creator.id


def _meta(video_id: str) -> VideoMeta:
    """带读数的 `VideoMeta`：这一族用例要的就是"读数进库、字节不进库"。"""
    meta = make_video_meta(PLATFORM, video_id)
    return meta.model_copy(
        update={
            "like_count": 1200,
            "view_count": 34000,
            "comment_count": 55,
            "duration_seconds": 61.0,
        }
    )


def _artifact(_meta: VideoMeta, dest: Path) -> SingleFileArtifact:
    path = dest / "media.mp4"
    path.write_bytes(b"x" * 32)
    return SingleFileArtifact(
        path=path, size_bytes=32, media_source="page_play_url", has_audio=True
    )


def _ctx(storage, files, video_ids: list[str]) -> TaskContext:
    adapter = FakeAdapter(
        PLATFORM,
        videos=[_meta(vid) for vid in video_ids],
        artifact_factory=_artifact,
    )
    return make_ctx(
        storage=storage,
        files=files,
        registry=FakeRegistry({PLATFORM: adapter}, {PLATFORM: FakeConfig()}),
        bus=FakeBus(),
    )


# --------------------------------------------------------------------------- #
# 只登记读数的那一趟
# --------------------------------------------------------------------------- #


async def test_metrics_only_registers_rows_without_touching_the_downloader(storage, files) -> None:
    await _seed_creator(storage)
    ctx = _ctx(storage, files, ["v1", "v2"])
    adapter = ctx.adapters.get(PLATFORM)

    result = await make_collect_handler(PLATFORM)(ctx, CollectParams(metrics_only=True))

    assert result.status == "success"
    assert adapter.download_calls == [], "metrics_only 却还是调了下载 = 配额照烧"
    assert result.summary["downloaded"] == 0
    assert result.summary["registered_metrics_only"] == 2
    # 清单里一个产物都不该有：不存在的路径进清单，产物核验那一栏会红得没道理
    assert result.artifacts == []
    assert await storage.videos.count() == 2


async def test_metrics_only_rows_say_there_was_never_media(storage, files) -> None:
    """`media_path` NULL + `metadata_json.media_downloaded=false`，两件事都要在。

    只有 NULL 的话，这一条与"下载失败但没记上原因"完全同形 —— 正是 AGENTS.md 硬约束 3
    禁止的那种假信号。
    """
    await _seed_creator(storage)
    ctx = _ctx(storage, files, ["v1"])
    await make_collect_handler(PLATFORM)(ctx, CollectParams(metrics_only=True))

    row = await storage.videos.find_by_platform_id(PLATFORM, "v1")
    assert row is not None
    assert row.media_path is None
    assert row.media_source is None
    assert json.loads(row.media_aux_paths_json) == []
    meta = json.loads(row.metadata_json)
    assert meta["media_downloaded"] is False
    assert meta["has_audio"] is False
    # 读数字段照旧进库：这一趟换来的就是这些数
    assert row.like_count == 1200
    assert row.view_count == 34000
    assert row.duration_seconds == 61.0


async def test_no_media_directory_is_created_for_metrics_only_rows(storage, files) -> None:
    """连目录都不建。留一个空目录的形状是"下载失败了"，而这里从没发起过下载。"""
    await _seed_creator(storage)
    ctx = _ctx(storage, files, ["v1"])
    await make_collect_handler(PLATFORM)(ctx, CollectParams(metrics_only=True))
    assert list(files.media_root.rglob("*")) == []


# --------------------------------------------------------------------------- #
# 与已有媒体的关系（这一族最容易写错的地方）
# --------------------------------------------------------------------------- #


async def test_a_metrics_only_pass_never_downgrades_rows_that_already_have_media(
    storage, files
) -> None:
    """查重在前：已经采过的作品，第二轮 `metrics_only` 一条都不许改。

    "没有产物"与"不要产物"在实现里只差一个分支顺序。症状是绿的：跑完"只采指标"，
    之前下好的媒体与稿子从界面上消失。
    """
    await _seed_creator(storage)
    first = await make_collect_handler(PLATFORM)(
        _ctx(storage, files, ["v1", "v2"]), CollectParams()
    )
    assert first.summary["downloaded"] == 2

    row = await storage.videos.find_by_platform_id(PLATFORM, "v1")
    assert row is not None and row.media_path

    second = await make_collect_handler(PLATFORM)(
        _ctx(storage, files, ["v1", "v2"]), CollectParams(metrics_only=True)
    )
    assert second.summary["downloaded"] == 0
    assert second.summary["registered_metrics_only"] == 0
    assert second.summary["skipped_existing"] == 2

    after = await storage.videos.find_by_platform_id(PLATFORM, "v1")
    assert after is not None
    assert after.media_path == row.media_path, "已有媒体被第二轮抹掉了"
    assert json.loads(after.metadata_json).get("media_downloaded") is not False


async def test_the_two_counters_partition_the_new_rows(storage, files) -> None:
    """关系：`downloaded + registered_metrics_only` == 这一趟**新增**的行数。

    两个计数器一旦有一边漏加（比如将来多一条"下得来但不落盘"的路），
    单独断"这轮 2 条"仍会对得上总数，而这一条不会。
    """
    await _seed_creator(storage)
    mixed = await make_collect_handler(PLATFORM)(
        _ctx(storage, files, ["v1", "v2", "v3"]), CollectParams()
    )
    assert mixed.summary["downloaded"] + mixed.summary["registered_metrics_only"] == 3
    assert await storage.videos.count() == 3

    second = await make_collect_handler(PLATFORM)(
        _ctx(storage, files, ["v1", "v2", "v3"]), CollectParams(metrics_only=True)
    )
    assert second.summary["downloaded"] + second.summary["registered_metrics_only"] == 0
    assert await storage.videos.count() == 3


# --------------------------------------------------------------------------- #
# 草稿本身与前端契约
# --------------------------------------------------------------------------- #


async def test_metrics_only_draft_keeps_the_audio_gate_shut(storage, files) -> None:
    """`artifact=None` 的草稿要让后处理那一步**干净地跳过**。

    这是 T6.8 与 ADR-0019 那个修复的接缝：`postprocess._audio_source` 认的是
    `metadata_json.has_audio is False`。这里断"字段真被写成那个值"，
    于是后处理侧改不了这一头、这一头也瞒不过那一头。
    """
    ctx = _ctx(storage, files, ["v1"])
    draft = build_video_draft(_meta("v1"), creator_id=None, artifact=None, ctx=ctx)
    meta = json.loads(draft.metadata_json)
    assert meta["has_audio"] is False
    assert meta["media_downloaded"] is False
    assert draft.media_path is None
    assert draft.title == _meta("v1").title


def test_the_flag_is_declared_in_the_params_json_schema() -> None:
    """前端表单是从 `params_schema` 自动渲染的：字段没 description 就等于没有。

    （AGENTS.md §6 的自检项"动的是 Pydantic 模型？→ JSON Schema 还能渲染表单"。）
    """
    schema = CollectParams.model_json_schema()
    assert "metrics_only" in schema["properties"]
    assert schema["properties"]["metrics_only"]["description"]
