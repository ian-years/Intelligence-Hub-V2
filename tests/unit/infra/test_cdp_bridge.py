"""CDP 桥客户端测试。

全部走 `httpx.MockTransport` —— **不碰真浏览器**。真机那一档在
`@pytest.mark.real_network`（Task 6/7），因为这里要钉的是"503 与连不上被区分开"
这种**语义**，与桥到底起没起无关。

V1 §7.20 的三条结论在这里全部变成断言：
1. `/health` 回 **503** = 桥在跑、浏览器没了；不是"桥没起"。
2. `bridge_available()` 对 503 必须返回 **True** —— 自愈只发生在第一条真请求上，
   预检因为 503 就退出等于被自己的健康检查堵死（V1 2026-09-21 真栽过）。
3. "浏览器已关闭"这类错必须**抛**出去（消化成 `ok:false` 的话自愈分支永远走不到）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from intelligence_hub_v2.errors import BridgeError
from intelligence_hub_v2.infra.cdp_bridge import MAX_JS_LENGTH, BridgeClient

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> BridgeClient:
    return BridgeClient(
        "http://127.0.0.1:3457", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


def _json(payload: dict[str, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


# ---------------------------------------------------------------------------
# /health 的三种状态
# ---------------------------------------------------------------------------


async def test_healthy_bridge_reports_page_url() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return _json({"ok": True, "page_url": "https://www.douyin.com/user/x"})

    health = await _client(handler).health()
    assert health.reachable is True
    assert health.browser_ok is True
    assert health.usable is True
    assert health.page_url == "https://www.douyin.com/user/x"


async def test_503_means_browser_dead_not_bridge_down() -> None:
    """这一条是整个模块的核心区分。合并成一位就会得到"桥没起"的误导文案。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return _json({"ok": False, "error": "Target page, context or browser has been closed"}, 503)

    health = await _client(handler).health()
    assert health.reachable is True, "端口上有东西在听"
    assert health.browser_ok is False
    assert health.needs_browser_restart is True
    assert "closed" in (health.error or "")
    assert health.page_url is None, "缓存的 page_url 不许冒充当前页面（V1 §7.20）"


async def test_bridge_available_is_true_on_503() -> None:
    """V1 2026-09-21 真栽过：预检看到 503 就说"请先启动 Chrome/CDP 代理"，
    而桥就在 3457 上跑着 —— 自愈只发生在第一条真请求上，被健康检查堵死了。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return _json({"ok": False}, 503)

    assert await _client(handler).bridge_available() is True


async def test_connection_refused_is_the_real_bridge_down() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        msg = "connection refused"
        raise httpx.ConnectError(msg)

    client = _client(handler)
    health = await client.health()
    assert health.reachable is False and health.browser_ok is False
    assert await client.bridge_available() is False
    assert "connection refused" in (health.error or "")


async def test_a_404_from_health_is_reachable_but_not_usable() -> None:
    """路径写错/版本不匹配时不能报"一切正常"。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return _json({"detail": "not found"}, 404)

    health = await _client(handler).health()
    assert health.reachable is True
    assert health.browser_ok is False


# ---------------------------------------------------------------------------
# 真请求
# ---------------------------------------------------------------------------


async def test_cookies_returns_the_list() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["domain"] == "douyin.com"
        return _json({"cookies": [{"name": "sessionid", "value": "abc"}]})

    assert await _client(handler).cookies("douyin.com") == [{"name": "sessionid", "value": "abc"}]


async def test_a_malformed_cookies_response_is_an_error_not_an_empty_list() -> None:
    """ "没取到 cookie"与"桥换了返回形状"必须分得开：后者会被当成前者，
    然后人去重新登录，而真正的原因是协议不匹配。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return _json({"result": []})

    with pytest.raises(BridgeError, match="缺 cookies"):
        await _client(handler).cookies("douyin.com")


async def test_dead_browser_during_navigate_raises_so_self_heal_can_run() -> None:
    """V1 的原始 bug：`handle_navigate` 把失败消化成 `{"ok": false}`，
    于是抖音那条 navigate + evaluate 路径 0.2s 就返回"导航失败：browser has been closed"，
    重建分支一次都没走到。"""
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return _json({"ok": False, "error": "Target page, context or browser has been closed"})

    with pytest.raises(BridgeError, match="浏览器已关闭"):
        await _client(handler).navigate("https://www.douyin.com/user/x")
    assert seen == ["/navigate"]


async def test_a_site_side_navigate_error_is_returned_not_raised() -> None:
    """反方向也要钉住：`ERR_CONNECTION_CLOSED` 是网站的问题，
    为它重建浏览器等于"给每个 ValueError 弹一个窗口"（V1 §7.20 的取舍）。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return _json({"ok": False, "error": "net::ERR_CONNECTION_CLOSED"})

    result = await _client(handler).navigate("https://example.com/x")
    assert result["ok"] is False


@pytest.mark.parametrize("url", ["", "javascript:alert(1)", "file:///etc/passwd", "www.douyin.com"])
async def test_navigate_rejects_non_http_urls(url: str) -> None:
    """只接受完整 http/https。放进桥里等于让浏览器去执行 `javascript:` URI。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        msg = "不该被调到"
        raise AssertionError(msg)

    with pytest.raises(ValueError, match="http/https"):
        await _client(handler).navigate(url)


async def test_evaluate_returns_the_result_field() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["expression"] == "document.title"
        return _json({"ok": True, "result": "抖音精选详情页"})

    assert await _client(handler).evaluate("document.title") == "抖音精选详情页"


async def test_evaluate_failure_carries_the_original_text() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return _json({"ok": False, "error": "ReferenceError: __VARS__ is not defined"})

    with pytest.raises(BridgeError, match="__VARS__ is not defined"):
        await _client(handler).evaluate("window.__VARS__")


async def test_an_oversized_script_is_refused_before_it_reaches_the_bridge() -> None:
    """模板拼坏了会产出几十 MB 的字符串。让桥 OOM 的代价是"整条链路报奇怪的错"，
    而在这里拒掉的代价是一行日志。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        msg = "不该被调到"
        raise AssertionError(msg)

    with pytest.raises(ValueError, match="超过上限"):
        await _client(handler).evaluate("x" * (MAX_JS_LENGTH + 1))


async def test_connection_error_on_a_real_request_becomes_a_bridge_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        msg = "refused"
        raise httpx.ConnectError(msg)

    with pytest.raises(BridgeError, match="桥没响应"):
        await _client(handler).evaluate("1")


# ---------------------------------------------------------------------------
# 构造与生命周期
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["127.0.0.1:3457", "https://", ""])
def test_only_http_urls_are_accepted_as_a_base(bad: str) -> None:
    """少了 scheme 会被 httpx 当成相对路径，症状是"所有请求 404"，离原因很远。"""
    with pytest.raises(ValueError, match="http/https"):
        BridgeClient(bad)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:3457",
        "http://localhost:3457",
        "http://[::1]:3457",
        "http://127.0.0.53:3457",
    ],
)
def test_loopback_bridge_urls_are_accepted(url: str) -> None:
    BridgeClient(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5:3457",  # 内网地址
        "http://bridge.example.com:3457",  # 公网主机名
        "http://127.0.0.1.evil.com:3457",  # 前缀匹配会放过的那种
        "http://0.0.0.0:3457",  # 全网卡
        "http://[::ffff:8.8.8.8]:3457",  # IPv4 映射在 IPv6 里
    ],
)
def test_non_loopback_bridge_urls_are_rejected_at_construction(url: str) -> None:
    """AGENTS §1.2：这个服务会在**人已登录的浏览器**里执行任意 JS。

    `BridgeClient` 原先只查 scheme + 有没有主机，而 `cdp_bridge.py:85` 的注释写着
    「预检会把非回环报成 degraded」—— 全 `src/` 没有那个预检，`tasks/preflight.py`
    一次都没提桥。于是改一行 `config/app.yaml` 的 `cdp_bridge.url` 就能把
    `evaluate`(我们注入的 JS)、`navigate`(博主主页)、`cookies`(取会话凭证) 三条请求
    送到远端，而**它的回复还会被当成页面内容写进库**：整条作品列表、播放直链、字幕
    都可以被伪造。所以这里在构造期就拒，且给的是 ValueError 而不是"降级"。
    """
    with pytest.raises(ValueError, match="回环"):
        BridgeClient(url)


async def test_a_shared_http_client_is_not_closed_by_the_bridge_client() -> None:
    """一个 httpx.AsyncClient 全平台共用（连接池）。关掉它会连带弄坏别的调用方。"""
    shared = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: _json({"ok": True})))
    client = BridgeClient("http://127.0.0.1:3457", shared)
    await client.aclose()
    assert not shared.is_closed
    await shared.aclose()


async def test_the_default_503_path_of_a_real_request_is_not_swallowed() -> None:
    """`/cookies` 回 503 也必须抛（自愈发生在第一条真请求上，见模块 docstring 第 2 条）。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return _json({"ok": False, "error": "browser has been closed"}, 503)

    with pytest.raises(BridgeError, match="503"):
        await _client(handler).cookies("douyin.com")
