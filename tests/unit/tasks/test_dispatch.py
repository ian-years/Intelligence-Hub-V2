"""`tasks/dispatch.py`：按链接认平台 + 反推作品 URL。"""

from __future__ import annotations

import pytest
from tests.unit.tasks.conftest import FakeAdapter, FakeConfig, FakeRegistry

from intelligence_hub_v2.errors import PlatformError
from intelligence_hub_v2.platforms import PLATFORM_CONFIG_SCHEMAS
from intelligence_hub_v2.platforms.bilibili.urls import (
    canonical_video_url as bili_canonical_video_url,
)
from intelligence_hub_v2.platforms.youtube.urls import (
    canonical_video_url as yt_canonical_video_url,
)
from intelligence_hub_v2.tasks.dispatch import (
    _VIDEO_URL_TEMPLATE,
    PLATFORM_HOST_SUFFIXES,
    canonical_video_url,
    detect_platform,
)


def _registry(*, douyin_enabled: bool = True, bilibili_enabled: bool = True) -> FakeRegistry:
    configs = {
        "douyin": FakeConfig(enabled=douyin_enabled),
        "bilibili": FakeConfig(enabled=bilibili_enabled),
    }
    adapters = {"douyin": FakeAdapter("douyin"), "bilibili": FakeAdapter("bilibili")}
    return FakeRegistry(adapters, configs)


def test_recognizes_douyin_and_bilibili_hosts() -> None:
    reg = _registry()
    assert detect_platform("https://www.douyin.com/user/abc", reg) == "douyin"
    assert detect_platform("https://space.bilibili.com/123", reg) == "bilibili"
    assert detect_platform("https://b23.tv/xyz", reg) == "bilibili"


def test_no_substring_false_positive_on_lookalike_domain() -> None:
    """`evil-douyin.com.attacker.net` 含 "douyin.com" 但不是它的子域，必须认不出。"""
    reg = _registry()
    with pytest.raises(PlatformError):
        detect_platform("https://evil-douyin.com.attacker.net/user/x", reg)


def test_host_without_scheme_still_resolves() -> None:
    reg = _registry()
    assert detect_platform("www.bilibili.com/video/BV1xx", reg) == "bilibili"


def test_disabled_platform_is_rejected() -> None:
    reg = _registry(douyin_enabled=False)
    with pytest.raises(PlatformError, match="没启用"):
        detect_platform("https://www.douyin.com/user/abc", reg)


def test_empty_host_raises() -> None:
    reg = _registry()
    with pytest.raises(PlatformError, match="主机名"):
        detect_platform("   ", reg)


def test_canonical_video_url_roundtrips() -> None:
    assert canonical_video_url("bilibili", "BV1xx") == "https://www.bilibili.com/video/BV1xx"
    assert canonical_video_url("douyin", "123") == "https://www.douyin.com/video/123"


def test_canonical_video_url_unknown_platform_raises() -> None:
    # 样本必须是**真的没注册**的平台。T2.1 之前用 xiaohongshu、T2.2 之前用 youtube，
    # 都是随手挑的一个"还不认识的名字"；两家注册进来之后它们就成了合法值 ——
    # 这条会退化成"测了个不存在的情况却仍然绿"。四家齐了就用假想名，
    # 并配一条防空转的前置（它真的不在表里）。
    assert "weibo" not in _VIDEO_URL_TEMPLATE
    with pytest.raises(PlatformError):
        canonical_video_url("weibo", "abc")


def test_the_dispatch_tables_cover_exactly_the_registered_platforms() -> None:
    """两张表都必须**正好**覆盖注册表里的那些平台。

    少一家 = 那一家的链接认不出平台（`detect_platform` 抛"主机不在已知平台里"），
    或多一家 = `canonical_video_url` 拼不出地址（`postprocess` 那条路会红）。
    判据取集合相等，不手写名单：名单每加一个平台就要改两处，早晚会分叉。
    """
    registered = set(PLATFORM_CONFIG_SCHEMAS)
    assert set(PLATFORM_HOST_SUFFIXES) == registered
    assert set(_VIDEO_URL_TEMPLATE) == registered


def test_youtube_links_are_recognized_on_every_host_form() -> None:
    """`youtu.be` 短链与 `youtube-nocookie.com` 嵌入域都算 YouTube。

    漏掉后者的症状很怪：同一条视频换个域名粘进来就变成"未知平台"。
    """
    registry = FakeRegistry(
        {"youtube": FakeAdapter("youtube")}, {"youtube": FakeConfig(enabled=True)}
    )
    for url in (
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "youtu.be/dQw4w9WgXcQ",
        "https://m.youtube.com/shorts/dQw4w9WgXcQ",
        "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
    ):
        assert detect_platform(url, registry) == "youtube", url


def test_canonical_video_url_matches_the_platforms_own_url_helper() -> None:
    """**关系判据**：dispatch 那张模板表与 YouTube 自己的规范 URL 必须一致。

    两处各写一遍迟早漂成"`postprocess` 拼出来的主页与采集时入库的不是同一条地址"，
    而那条地址会进 `fetch_subtitles` 与前端外链。

    B站 那一行**故意不相等**：`bilibili/urls.canonical_video_url` 带尾斜杠
    （它的 docstring 写明"不带时 yt-dlp 会先做一次 301"），而 dispatch 的模板不带。
    这是既有的差异，不在本格里统一 —— 统一它要拿 B站 的用例当证据另做。
    """
    assert canonical_video_url("youtube", "dQw4w9WgXcQ") == yt_canonical_video_url("dQw4w9WgXcQ")
    bili = canonical_video_url("bilibili", "BV1GJ411x7h7")
    assert bili_canonical_video_url("BV1GJ411x7h7") == f"{bili}/"
