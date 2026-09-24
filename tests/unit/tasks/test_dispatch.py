"""`tasks/dispatch.py`：按链接认平台 + 反推作品 URL。"""

from __future__ import annotations

import pytest
from tests.unit.tasks.conftest import FakeAdapter, FakeConfig, FakeRegistry

from intelligence_hub_v2.errors import PlatformError
from intelligence_hub_v2.tasks.dispatch import canonical_video_url, detect_platform


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
    # 样本必须是**真的没注册**的平台。T2.1 之前用 xiaohongshu 是随手挑的一个"还不认识的名字"，
    # 小红书注册进来之后它就成了合法值 —— 这条会退化成"测了个不存在的情况却仍然绿"。
    # 换成 youtube：它是下一个要落地的（T2.2），到时候这一格还得再翻一次，那是设计好的红。
    with pytest.raises(PlatformError):
        canonical_video_url("youtube", "abc")
