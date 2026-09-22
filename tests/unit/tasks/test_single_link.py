"""`tasks/single_link.py`：收一条作品链接（V2.0 最小实现）。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from tests.unit.tasks.conftest import FakeAdapter, FakeBus, FakeConfig, FakeRegistry, make_ctx

from intelligence_hub_v2.errors import PlatformError, TaskCancelled
from intelligence_hub_v2.models.event import EventType
from intelligence_hub_v2.models.media import SingleFileArtifact
from intelligence_hub_v2.tasks.params import SingleLinkParams
from intelligence_hub_v2.tasks.single_link import run_single_link


def _artifact(video, dest: Path) -> SingleFileArtifact:
    path = dest / "media.mp4"
    path.write_bytes(b"x" * 16)
    return SingleFileArtifact(path=path, size_bytes=16, media_source="yt_dlp")


def _reg(platform: str) -> FakeRegistry:
    adapter = FakeAdapter(platform, artifact_factory=_artifact)
    return FakeRegistry({platform: adapter}, {platform: FakeConfig()})


async def test_bilibili_full_link_downloads_with_no_creator(storage, files) -> None:
    reg = _reg("bilibili")
    bus = FakeBus()
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=bus)

    result = await run_single_link(
        ctx, SingleLinkParams(url="https://www.bilibili.com/video/BV1xx411c7mD")
    )

    assert result.status == "success"
    assert result.summary["platform_video_id"] == "BV1xx411c7mD"
    row = await storage.videos.find_by_platform_id("bilibili", "BV1xx411c7mD")
    assert row is not None
    assert row.creator_id is None  # 作品链接不含博主身份，硬凑一个就是脏数据
    assert len([e for e in bus.events if e.type is EventType.VIDEO_ADDED]) == 1


async def test_douyin_numeric_link(storage, files) -> None:
    reg = _reg("douyin")
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())

    result = await run_single_link(
        ctx, SingleLinkParams(url="https://www.douyin.com/video/7413800")
    )

    assert result.summary["platform"] == "douyin"
    assert result.summary["platform_video_id"] == "7413800"


async def test_modal_id_query_param_is_recognized(storage, files) -> None:
    reg = _reg("douyin")
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())

    result = await run_single_link(
        ctx, SingleLinkParams(url="https://www.douyin.com/user/x?modal_id=7413800")
    )

    assert result.summary["platform_video_id"] == "7413800"


async def test_short_link_is_expanded_before_parsing(storage, files) -> None:
    """b23.tv 短链要先跟 302，落地页里才有 BV 号（V1 §7.1 的同类跳转，对象换成作品）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if "b23.tv" in request.url.host:
            return httpx.Response(
                302, headers={"location": "https://www.bilibili.com/video/BV1gqQmYZEc7"}
            )
        return httpx.Response(200, json={})

    reg = _reg("bilibili")
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())
    await ctx.deps.http.aclose()
    ctx.deps.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)

    result = await run_single_link(ctx, SingleLinkParams(url="https://b23.tv/abc"))

    assert result.summary["platform_video_id"] == "BV1gqQmYZEc7"


async def test_link_without_parseable_id_raises_with_original_in_message(storage, files) -> None:
    reg = _reg("bilibili")
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())

    with pytest.raises(PlatformError, match="认不出作品 id"):
        await run_single_link(ctx, SingleLinkParams(url="https://www.bilibili.com/video/"))


async def test_re_run_is_not_created_again(storage, files) -> None:
    reg = _reg("bilibili")
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())
    params = SingleLinkParams(url="https://www.bilibili.com/video/BV1xx411c7mD")

    await run_single_link(ctx, params)
    second = await run_single_link(ctx, params)

    assert second.summary["created"] == 0
    assert await storage.videos.count() == 1


async def test_cancelled_before_start(storage, files) -> None:
    reg = _reg("bilibili")
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())
    ctx.cancel_token.cancel()

    with pytest.raises(TaskCancelled):
        await run_single_link(
            ctx, SingleLinkParams(url="https://www.bilibili.com/video/BV1xx411c7mD")
        )
