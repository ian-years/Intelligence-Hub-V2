"""集成：任务从 runner 走到入库 + 清单落盘的完整链路（Task 8 的端到端看护）。

用真 `TaskRunner` + 真 `manifest_writer` + 真 SQLite / 真文件目录，只把**外部世界**
（平台适配器）换成假件 —— 这正是 V2.0 缺真机 cookie/桥时仍能验的那一层：
"任务 → 采集 → 入库 → 清单"的接线对不对，不依赖网络。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.tasks.conftest import (
    FakeAdapter,
    FakeBus,
    FakeConfig,
    FakeDepsFactory,
    FakeLogger,
    FakeRegistry,
    make_deps,
    make_video_meta,
)

from intelligence_hub_v2.core.config import AppConfig
from intelligence_hub_v2.core.task_registry import get_task
from intelligence_hub_v2.core.task_runner import TaskRunner
from intelligence_hub_v2.errors import TaskCancelled
from intelligence_hub_v2.models.creator import CreatorDraft, CreatorProfile, CreatorRef
from intelligence_hub_v2.models.event import EventType
from intelligence_hub_v2.models.media import SingleFileArtifact
from intelligence_hub_v2.tasks.definition import CancelToken
from intelligence_hub_v2.tasks.params import AddCreatorParams, CollectParams

pytestmark = pytest.mark.integration

PLATFORM = "bilibili"


def _artifact(video, dest: Path) -> SingleFileArtifact:
    path = dest / "media.mp4"
    path.write_bytes(b"media-bytes")
    return SingleFileArtifact(
        path=path,
        size_bytes=len(b"media-bytes"),
        media_source="yt_dlp",
        cookie_rung="带导出的登录 cookie",
    )


def _make_runner(storage, files, registry, bus) -> TaskRunner:
    return TaskRunner(
        storage=storage,
        events=bus,  # type: ignore[arg-type]
        files=files,
        registry=registry,  # type: ignore[arg-type]
        deps_factory=FakeDepsFactory(make_deps(FakeConfig())),  # type: ignore[arg-type]
        app_config=AppConfig(),
        logger=FakeLogger(),  # type: ignore[arg-type]
    )


async def test_collect_flow_stores_videos_and_writes_manifest(storage, files) -> None:
    await storage.creators.insert(
        CreatorDraft(
            platform=PLATFORM,
            platform_id="mid1",
            name="某UP",
            profile_url="https://space.bilibili.com/mid1",
            is_tracking=True,
        )
    )
    adapter = FakeAdapter(
        PLATFORM,
        videos=[make_video_meta(PLATFORM, "BV1"), make_video_meta(PLATFORM, "BV2")],
        artifact_factory=_artifact,
    )
    reg = FakeRegistry({PLATFORM: adapter}, {PLATFORM: FakeConfig()})
    bus = FakeBus()
    runner = _make_runner(storage, files, reg, bus)
    definition = get_task("bilibili_collect")

    await runner.execute(
        definition,
        task_id="flow-1",
        params=CollectParams(),
        config_snapshot={},
        cancel_token=CancelToken(),
    )

    record = await storage.task_runs.get_or_raise("flow-1")
    assert record.status == "success"
    assert record.manifest_path is not None
    assert await storage.videos.count() == 2
    assert len([e for e in bus.events if e.type is EventType.VIDEO_ADDED]) == 2

    # 清单文件是权威源，内容里带着逐条产物
    manifest = (await storage.manifests.latest_for_task("flow-1")).content()  # type: ignore[union-attr]
    assert manifest.status == "success"
    assert manifest.summary["downloaded"] == 2
    assert len(manifest.artifacts) == 2
    assert files.abs(record.manifest_path).is_file()


async def test_add_creator_then_collect_same_creator(storage, files) -> None:
    ref = CreatorRef.model_validate(
        {
            "platform": PLATFORM,
            "platform_id": "mid1",
            "profile_url": "https://space.bilibili.com/mid1",
        }
    )
    profile = CreatorProfile.model_validate({"ref": ref, "name": "某UP"})
    adapter = FakeAdapter(
        PLATFORM,
        ref=ref,
        profile=profile,
        videos=[make_video_meta(PLATFORM, "BV1")],
        artifact_factory=_artifact,
    )
    reg = FakeRegistry({PLATFORM: adapter}, {PLATFORM: FakeConfig()})
    runner = _make_runner(storage, files, reg, FakeBus())

    await runner.execute(
        get_task("add_creator"),
        task_id="ac-1",
        params=AddCreatorParams(url="https://space.bilibili.com/mid1", platform=PLATFORM),
        config_snapshot={},
        cancel_token=CancelToken(),
    )
    creator = await storage.creators.find(PLATFORM, "mid1")
    assert creator is not None and creator.is_tracking

    await runner.execute(
        get_task("bilibili_collect"),
        task_id="col-1",
        params=CollectParams(creator_ids=[creator.id]),
        config_snapshot={},
        cancel_token=CancelToken(),
    )
    assert await storage.videos.count() == 1


async def test_second_collect_run_is_incremental(storage, files) -> None:
    await storage.creators.insert(
        CreatorDraft(
            platform=PLATFORM,
            platform_id="mid1",
            name="某UP",
            profile_url="https://x",
            is_tracking=True,
        )
    )
    adapter = FakeAdapter(
        PLATFORM, videos=[make_video_meta(PLATFORM, "BV1")], artifact_factory=_artifact
    )
    reg = FakeRegistry({PLATFORM: adapter}, {PLATFORM: FakeConfig()})
    runner = _make_runner(storage, files, reg, FakeBus())

    await runner.execute(
        get_task("bilibili_collect"),
        task_id="i1",
        params=CollectParams(),
        config_snapshot={},
        cancel_token=CancelToken(),
    )
    await runner.execute(
        get_task("bilibili_collect"),
        task_id="i2",
        params=CollectParams(),
        config_snapshot={},
        cancel_token=CancelToken(),
    )

    second = (await storage.manifests.latest_for_task("i2")).content()  # type: ignore[union-attr]
    assert second.summary["downloaded"] == 0
    assert second.summary["skipped_existing"] == 1
    assert await storage.videos.count() == 1


async def test_cancel_mid_collect_still_writes_cancelled_manifest(storage, files) -> None:
    await storage.creators.insert(
        CreatorDraft(
            platform=PLATFORM,
            platform_id="mid1",
            name="某UP",
            profile_url="https://x",
            is_tracking=True,
        )
    )
    adapter = FakeAdapter(
        PLATFORM, videos=[make_video_meta(PLATFORM, "BV1")], artifact_factory=_artifact
    )
    reg = FakeRegistry({PLATFORM: adapter}, {PLATFORM: FakeConfig()})
    runner = _make_runner(storage, files, reg, FakeBus())
    token = CancelToken()
    token.cancel()

    with pytest.raises(TaskCancelled):
        await runner.execute(
            get_task("bilibili_collect"),
            task_id="cx",
            params=CollectParams(),
            config_snapshot={},
            cancel_token=token,
        )

    record = await storage.task_runs.get_or_raise("cx")
    assert record.status == "cancelled"
    assert record.ended_at is not None
