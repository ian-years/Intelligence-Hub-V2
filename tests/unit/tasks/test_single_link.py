"""`tasks/single_link.py`：收一条作品链接（V2.0 最小实现）。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from tests.unit.tasks.conftest import FakeAdapter, FakeBus, FakeConfig, FakeRegistry, make_ctx

from intelligence_hub_v2.errors import PlatformError, TaskCancelled
from intelligence_hub_v2.models.event import EventType
from intelligence_hub_v2.models.media import SingleFileArtifact
from intelligence_hub_v2.models.video import VideoMeta
from intelligence_hub_v2.tasks.params import SingleLinkParams
from intelligence_hub_v2.tasks.single_link import run_single_link

#: 小红书笔记 id 的形状是 24 位 hex（`extract_note_id` 认的就是它）。
_NOTE_ID = "6a910d0400000000210337a9"


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
    """查重必须发生在**下载之前**（review P1-10）：原来是无脑下载完才 insert_or_get
    —— 同一条链接点两次 = 两次完整下载 + 第二次覆盖第一次的产物（带宽与风控都是成本），
    summary 却报 `created=0` 仍 `success`。"""
    reg = _reg("bilibili")
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())
    params = SingleLinkParams(url="https://www.bilibili.com/video/BV1xx411c7mD")

    await run_single_link(ctx, params)
    second = await run_single_link(ctx, params)

    assert second.summary["created"] == 0
    assert second.summary["skipped"] == 1
    assert await storage.videos.count() == 1
    adapter = reg.get("bilibili")
    assert adapter.download_calls == ["BV1xx411c7mD"], "第二趟不该再下载"


async def test_cancelled_before_start(storage, files) -> None:
    reg = _reg("bilibili")
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())
    ctx.cancel_token.cancel()

    with pytest.raises(TaskCancelled):
        await run_single_link(
            ctx, SingleLinkParams(url="https://www.bilibili.com/video/BV1xx411c7mD")
        )


async def test_a_xhs_link_keeps_its_xsec_token_for_the_detail_step(storage, files) -> None:
    """粘进来的小红书链接，那个 `xsec_token` 必须一路带到 `download_media`。

    这是 T3.3 真机跑出来的 bug：handler 把链接压成 `canonical_video_url(platform, id)`
    的同时**把 query 参数一起压掉了**，于是详情页拿到空 token、站内跳风控页，
    症状与"笔记被删/登录过期"三者同形。同一条笔记手工导航能正常打开，
    所以不是现网的锅（2026-09-25 当场对拍过）。
    断言的是"适配器**收到**的那个 meta 里 token 还在"，不是"我拼的字符串里有"：
    压成 canonical 那一步就是丢它的地方。
    """
    token = "ABOgq79sds9_-oBYa6uGqmDJ6KcKC_sWjr37ssyquu0iw="
    url = f"https://www.xiaohongshu.com/explore/{_NOTE_ID}?xsec_token={token}&xsec_source=pc_feed"
    seen: list[VideoMeta] = []

    def capture(video: VideoMeta, dest: Path) -> SingleFileArtifact:
        # 同步工厂：`FakeAdapter.download_media` 直接把它返回值当产物用
        seen.append(video)
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / "media.mp4"
        path.write_bytes(b"x" * 16)
        return SingleFileArtifact(
            path=path, size_bytes=16, media_source="page_play_url", has_audio=True
        )

    adapter = FakeAdapter("xiaohongshu", artifact_factory=capture, download_error=None)
    reg = FakeRegistry({"xiaohongshu": adapter}, {"xiaohongshu": FakeConfig()})
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())

    await run_single_link(ctx, SingleLinkParams(url=url))

    assert seen, "download_media 没被调用，这条就在测空气"
    assert seen[0].extra.get("xsec_token") == token, f"token 在链路上丢了：{seen[0].extra!r}"
    # 但 `webpage_url` 仍是规范主页（库里与清单里不该出现会过期的分享链）
    assert "xsec_token" not in str(seen[0].webpage_url)
