"""CDP 桥端到端：真 HTTP 往返 + 真 `BridgeHandler` / 真 `BridgeClient`，只有浏览器是假的。

为什么这一档不能用 mock 顶替：客户端与服务端各自实现了一半契约，
"客户端多发了一个字段而服务端当没看见"、"服务端换了返回形状而客户端还在按老键取"
这两类漂移只有对着一真的服务才当场红。2026-09-24 核对 T0.1 时撞到的正是后一种：
V1 的 `/cookies` 只回 `netscape` 文本，而 `BridgeClient.cookies()` 要 `cookies` 列表
（照搬会让 cookie 那条路恒抛"返回里缺 cookies 列表"）。

计划里那条"桥在 / 桥不在两种态"在这里是两条用例，再加第三种状态
"桥在但浏览器没了"（V1 §7.20 的红绿灯区分，也是这一族最容易判错的一位）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from tests._bridge_doubles import (
    BROWSER_DEAD,
    COOKIE_SAMPLE,
    FakeContext,
    FakePage,
    FakePlaywright,
    ThreadlessWorker,
)

from intelligence_hub_v2.bridge.server import MAX_BODY_BYTES, BridgeHandler
from intelligence_hub_v2.infra.cdp_bridge import MAX_JS_LENGTH, BridgeClient, BridgeHealth
from intelligence_hub_v2.infra.cookies import COOKIE_FILE_HEADER, CookieManager
from intelligence_hub_v2.storage.files import FileStorage

REPO_ROOT = Path(__file__).resolve().parents[2]
Bridge = tuple[ThreadlessWorker, str]


def _async_client() -> httpx.AsyncClient:
    """测试专用的 httpx client。

    本机实测 `httpx.AsyncClient()` 一次要 ~2.0 秒（默认 transport 会去加载 Windows 证书库），
    13 条用例就是 26 秒。给一个**空的** SSLContext 就绕开那次加载：校验开着
    （`CERT_REQUIRED` + `check_hostname`），只是没有任何受信根，所以 https 一律失败关闭 ——
    而这里只说 `http://`、只连 127.0.0.1，TLS 全程不参与。
    **生产代码不受影响**：`BridgeClient` 自己建的仍是默认 transport。
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(verify=context))


@pytest.fixture
def bridge(tmp_path: Path) -> Iterator[Bridge]:
    """真的 `BridgeHandler` 挂在 127.0.0.1 的随机端口上，只有浏览器那一端是假句柄。"""
    page = FakePage(url="https://www.douyin.com/user/MS4")
    worker = ThreadlessWorker(
        playwright=FakePlaywright(),
        profile=tmp_path / "profile",
        headless=True,
        channel="chrome",
        timeout=5.0,
        settle_ms=0,
        restart_cooldown_seconds=0.0,
    )
    worker.page = page
    worker.context = FakeContext(pages=(page,), cookies=COOKIE_SAMPLE)
    handler = type("BoundTestHandler", (BridgeHandler,), {"worker": worker})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    yield worker, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


@pytest.fixture
async def api(bridge: Bridge) -> AsyncIterator[BridgeClient]:
    """同一个端口的真客户端。`BridgeClient` 只认回环，所以这里连的必须是 127.0.0.1。"""
    client = BridgeClient(bridge[1], _async_client())
    try:
        yield client
    finally:
        await client.aclose()


def _closed_port() -> int:
    """拿一个刚刚还空着、现在没人听的端口（"桥没起"与"桥 503"必须是两个答案）。"""
    with contextlib.closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --------------------------------------------------------------------------- #
# 桥的三种状态：活 / 浏览器没了 / 压根没起
# --------------------------------------------------------------------------- #


async def test_a_live_bridge_is_usable_and_names_its_page(api: BridgeClient) -> None:
    health = await api.health()
    assert health.status_code == 200
    assert health.reachable and health.browser_ok and health.usable
    assert health.page_url == "https://www.douyin.com/user/MS4"


async def test_a_dead_browser_is_503_not_bridge_down(api: BridgeClient, bridge: Bridge) -> None:
    """§7.20 的那一位区分跨了真 HTTP 仍然要成立：合并成一位就得到"请先启动桥"的假文案。"""
    worker, _ = bridge
    worker.context = FakeContext(dead=BROWSER_DEAD)

    health = await api.health()
    assert health.status_code == 503
    assert health.reachable is True
    assert health.browser_ok is False
    assert health.needs_browser_restart is True
    assert BROWSER_DEAD in (health.error or "")
    assert health.page_url is None  # 探不到就不给"当前页面"，别拿缓存值冒充


async def test_bridge_available_survives_a_dead_browser(api: BridgeClient, bridge: Bridge) -> None:
    """自愈只发生在第一条真请求上：预检因为 503 就退出，等于被自己的健康检查堵死。"""
    worker, _ = bridge
    worker.context = FakeContext(dead=BROWSER_DEAD)
    assert await api.bridge_available() is True


async def test_nothing_listening_is_a_different_answer() -> None:
    """端口上没东西在听 ≠ 桥回了 503。这里刻意用默认超时：要的就是真 `ConnectError` 这条路。"""
    client = BridgeClient(f"http://127.0.0.1:{_closed_port()}", _async_client())
    try:
        health = await client.health()
        assert health.reachable is False
        assert health.browser_ok is False
        assert health.status_code is None
        assert health.error  # "连不上"的原文要留着，预检的 detail 靠它
        # `bridge_available()` 就是 `health().reachable` 那一问，用例已覆盖，不重复连一次
        # （Windows 上对空端口发起 TCP 连接实测要 ~4 秒）。
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# /cookies：服务端 → V2 那唯一的 Netscape 渲染器，同一条链
# --------------------------------------------------------------------------- #


async def test_cookies_survive_the_wire_and_feed_the_renderer(
    api: BridgeClient, tmp_path: Path
) -> None:
    """桥只给列表，Netscape 的写法在 V2 只有 `infra/cookies.py` 一处。"""
    cookies = await api.cookies("douyin.com")
    assert [c["name"] for c in cookies] == ["sessionid", "ttwid"]

    path = CookieManager(FileStorage(tmp_path / "data")).write_netscape("douyin.com", cookies)
    text = path.read_text(encoding="utf-8")
    assert text.startswith(COOKIE_FILE_HEADER)
    body = [line for line in text.splitlines() if not line.startswith("#")]
    assert len(body) == 2
    session = next(line for line in body if line.split("\t")[5] == "ttwid").split("\t")
    assert session[0] == ".douyin.com" and session[1] == "TRUE"
    assert session[4] == "0"  # Playwright 的 -1（会话）在 Netscape 里是 0，-1 会被当过期丢掉


# --------------------------------------------------------------------------- #
# /navigate 与 /evaluate：客户端发的字段真的落到了页面
# --------------------------------------------------------------------------- #


async def test_navigate_forwards_wait_until(api: BridgeClient, bridge: Bridge) -> None:
    worker, _ = bridge
    out = await api.navigate("https://www.douyin.com/user/other", wait_until="networkidle")
    assert out["ok"] is True
    assert worker.page.visited[-1] == "https://www.douyin.com/user/other#{networkidle}"


async def test_evaluate_returns_the_page_value(api: BridgeClient, bridge: Bridge) -> None:
    worker, _ = bridge
    assert await api.evaluate("async () => 1 + 1") == {"ran": "async () => 1 + 1"}
    assert worker.page.evaluated == ["async () => 1 + 1"]


async def test_the_first_real_request_revives_a_closed_browser(
    api: BridgeClient, bridge: Bridge
) -> None:
    """人在窗口上点了关闭 → 下一条请求就地用同一个 profile 重建并重试，调用方不该看到红。"""
    worker, _ = bridge
    dead_page = FakePage(url="https://www.douyin.com/user/A", evaluate_error=BROWSER_DEAD)
    worker.page = dead_page
    worker.context = FakeContext(pages=(dead_page,), dead=BROWSER_DEAD)

    assert await api.evaluate("() => 1") == {"ran": "() => 1"}
    assert dead_page.evaluated == []  # 死页面上一次 JS 都没跑
    assert worker.restarts == 1
    assert worker.playwright.launches[0]["dir"] == str(worker.profile)  # §7.18 同一个 profile

    revived = await api.health()
    assert revived.browser_ok is True and revived.status_code == 200


async def test_a_site_side_failure_stays_a_structured_error(
    api: BridgeClient, bridge: Bridge
) -> None:
    """网站侧报错（不是浏览器没了）走结构化失败，不触发重建 —— 弹个新窗口也救不回来。"""
    worker, _ = bridge
    worker.page = FakePage(goto_error="ERR_CONNECTION_CLOSED")
    worker.context = FakeContext(pages=(worker.page,), cookies=COOKIE_SAMPLE)

    out = await api.navigate("https://www.douyin.com/user/B")
    assert out["ok"] is False
    assert "ERR_CONNECTION_CLOSED" in out["error"]
    assert worker.restarts == 0


# --------------------------------------------------------------------------- #
# HTTP 外壳
# --------------------------------------------------------------------------- #


async def test_only_the_documented_routes_exist(bridge: Bridge) -> None:
    async with _async_client() as http:
        assert (await http.get(f"{bridge[1]}/nope")).status_code == 404
        assert (await http.post(f"{bridge[1]}/evaluate", content="not json")).status_code == 400
        wrong_method = await http.post(f"{bridge[1]}/cookies", json={"domain": "douyin.com"})
        assert wrong_method.status_code == 404


async def test_an_oversized_body_is_refused_without_reading_half_of_it(
    bridge: Bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """读掉一半把剩下的留在 socket 里，下一条 keep-alive 请求就从没见过的字节开始。"""
    monkeypatch.setattr("intelligence_hub_v2.bridge.server.MAX_BODY_BYTES", 64)
    async with _async_client() as http:
        response = await http.post(f"{bridge[1]}/evaluate", json={"expression": "x" * 300})
    assert response.status_code == 413
    assert "上限" in response.json()["error"]


def test_the_body_cap_is_above_the_js_length_the_client_will_send() -> None:
    """客户端允许注入的上限要是比服务端的请求体上限还大，采集就会在半途吃到一个 413。"""
    assert MAX_JS_LENGTH < MAX_BODY_BYTES


async def test_a_fatal_worker_comes_back_as_a_structured_failure(
    bridge: Bridge, api: BridgeClient
) -> None:
    """`submit()` 抛（缺 Playwright / 线程已退）时服务端回 200 + `ok:false`，
    而 `result` 仍是页面脚本能 `JSON.parse` 的那句 —— 客户端照旧交出结构化失败，
    不是"HTTP 200 所以成功"。"""
    worker, base = bridge
    worker.fatal = "未安装 Playwright（uv sync --extra bridge）"

    async with _async_client() as http:
        response = await http.post(f"{base}/navigate", json={"url": "https://example.com/"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False and "Playwright" in body["error"]
    assert json.loads(body["result"])["ok"] is False

    out = await api.navigate("https://example.com/")
    assert out["ok"] is False and "Playwright" in out["error"]


# --------------------------------------------------------------------------- #
# 真 Chrome（默认不跑：`make test-real` / `pytest -m real_network`）
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def _static_page() -> Iterator[str]:
    """另一个端口上的一个真页面：浏览器到得了，又不会回头敲桥自己。

    为什么不用桥的 `/health` 当导航目标（那是最省事的写法）：**会互锁**。
    浏览器发出的那条请求要经 `BridgeHandler` → `worker.submit()` → 同一条请求队列，
    而队列里排在它前面的正是等这次导航完成的 `/navigate` —— 单线程 worker 永远轮不到它。
    2026-09-24 真机第一次跑就是这么 45 秒超时的，量出来才看清这条约束。
    """

    class Page(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b"<html><head><title>cdp-bridge-probe</title></head><body>hi</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            del fmt, args

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    httpd.daemon_threads = True
    threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/probe"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.real_network
async def test_a_real_chrome_round_trip_through_the_client(tmp_path: Path) -> None:
    """`python -m intelligence_hub_v2.bridge.server` 起真 Chrome，用**真 `BridgeClient`** 走一遍。

    这一段验的是"真 Playwright + 真 Chrome + 真 profile 目录 + 真 HTTP + 真 JS 求值 +
    适配器实际用的那个客户端"整条链。不验外网可达性，也不验登录态 ——
    抖音/B站 那一档在 T3.1/T3.2，要人先在桥里扫码登录。
    """
    port = _closed_port()
    base = f"http://127.0.0.1:{port}"
    with _static_page() as page_url:
        # 冒烟用例里没有并发要保护：阻塞地起进程比 create_subprocess_exec + 手工读管道好读。
        proc = subprocess.Popen(  # noqa: S603,ASYNC220 - argv 全是本文件写死的常量与 tmp 路径
            [
                sys.executable,
                "-X",
                "utf8",
                "-m",
                "intelligence_hub_v2.bridge.server",
                "--headless",
                "--port",
                str(port),
                "--profile",
                str(tmp_path / "profile"),
            ],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        client = BridgeClient(base, _async_client())
        try:
            health: BridgeHealth | None = None
            deadline = time.time() + 180
            while time.time() < deadline:
                if proc.poll() is not None:
                    pytest.fail(
                        "桥进程退了，退出码 "
                        f"{proc.returncode}：\n{(proc.stdout or '').read()[-2000:]}"
                    )
                candidate = await client.health()  # 不抛：连不上就是 reachable=False
                if candidate.usable:
                    health = candidate
                    break
                await asyncio.sleep(2.0)
            if health is None:
                pytest.fail(
                    "180 秒内没等到真 Chrome 把 /health 回绿（本机没装 Chrome 就是这个结果）"
                )
            assert health.status_code == 200
            assert health.page_url  # 真页面上读到的当前 URL，不是缓存字段
            assert (tmp_path / "profile").is_dir()  # profile 目录是桥自己建的

            landed = await client.navigate(page_url)
            assert landed["ok"] is True, landed
            assert landed["url"] == page_url

            # 真页面里真求值：这一条同时证明 expression 没被二次包装（异步箭头函数那套约定）
            assert await client.evaluate("() => document.title") == "cdp-bridge-probe"

            # 真 context 上的 /cookies：列表（V1 那份 netscape 文本已经不再是契约）
            assert await client.cookies("probe.invalid") == []

            after = await client.health()
            assert after.browser_ok is True
            assert after.page_url == page_url  # "桥现在停在哪一页"，真浏览器给的答案
        finally:
            await client.aclose()
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=15)
            if proc.stdout is not None:
                # 显式关管道：留给 GC 收尾的话，解释器退出时的 ResourceWarning 会被
                # `filterwarnings = error` 变成一条 unraisable 异常，整跑退出码非 0。
                proc.stdout.close()
