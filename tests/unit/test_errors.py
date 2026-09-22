"""错误层次测试。"""

import pytest

from intelligence_hub_v2.errors import (
    BridgeError,
    ConfigError,
    CookieError,
    IntelligenceHubError,
    ListError,
    MediaDownloadError,
    PlatformError,
    TaskCancelled,
    TaskRejected,
)


def test_platform_error_carries_context():
    err = PlatformError("douyin", "download", "yt-dlp failed", cause=ValueError("bad"))
    assert err.platform == "douyin"
    assert err.stage == "download"
    assert "yt-dlp failed" in str(err)
    assert isinstance(err.cause, ValueError)
    assert isinstance(err, IntelligenceHubError)


def test_cookie_error_is_platform_error():
    err = CookieError("bilibili", "cookies", "expired")
    assert isinstance(err, PlatformError)
    assert isinstance(err, IntelligenceHubError)


def test_task_cancelled_is_not_platform_error():
    err = TaskCancelled("user requested")
    assert isinstance(err, IntelligenceHubError)
    assert not isinstance(err, PlatformError)


@pytest.mark.parametrize("exc_cls", [BridgeError, MediaDownloadError, ListError])
def test_platform_error_subclasses(exc_cls):
    err = exc_cls("douyin", "list", "msg")
    assert isinstance(err, PlatformError)


def test_task_rejected():
    err = TaskRejected("platform disabled")
    assert isinstance(err, IntelligenceHubError)


def test_config_error():
    err = ConfigError("invalid yaml", path="config/app.yaml")
    assert err.path == "config/app.yaml"
    assert isinstance(err, IntelligenceHubError)
