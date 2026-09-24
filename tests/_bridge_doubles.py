"""CDP 桥的假 Playwright 句柄（服务端测试共用，不是用例，所以不以 `test_` 开头）。

真浏览器不在这里出现：这一层要能验的是"浏览器没了必须往外抛""重建必须沿用同一个
profile""导航失败必须擦页面"这类**分支形状**，它们与本机有没有 Chrome 无关。
真 Chrome 那一档在 `tests/integration/test_bridge_health.py` 的 `real_network` 用例里。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from intelligence_hub_v2.bridge.server import BrowserWorker

BROWSER_DEAD = "Target page, context or browser has been closed"
"""Playwright 在浏览器被关掉后的原文之一。判定只认这几句，见 `BROWSER_DEAD_MARKERS`。"""

COOKIE_SAMPLE: list[dict[str, Any]] = [
    {
        "name": "sessionid",
        "value": "abc",
        "domain": ".douyin.com",
        "path": "/",
        "secure": True,
        "expires": 1_900_000_000,
    },
    {
        "name": "ttwid",
        "value": "xyz",
        "domain": ".douyin.com",
        "path": "/",
        "secure": False,
        "expires": -1,  # 会话 cookie：Playwright 给 -1，Netscape 要 0
    },
]


class FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status


class FakePage:
    """`goto_error` / `evaluate_error` 非空 = 那一步抛这句（`about:blank` 除外，那是擦页面）。"""

    def __init__(
        self, *, url: str = "about:blank", goto_error: str = "", evaluate_error: str = ""
    ) -> None:
        self.url = url
        self.goto_error = goto_error
        self.evaluate_error = evaluate_error
        self.visited: list[str] = []
        self.evaluated: list[str] = []
        self.waited_ms: list[int] = []
        self.default_timeout_ms: int | None = None

    def set_default_timeout(self, ms: float) -> None:
        self.default_timeout_ms = ms

    def goto(
        self, url: str, wait_until: str = "domcontentloaded", timeout: float = 0
    ) -> FakeResponse:
        if self.goto_error and url != "about:blank":
            raise RuntimeError(self.goto_error)
        self.url = url
        self.visited.append(f"{url}#{{{wait_until}}}")
        return FakeResponse()

    def wait_for_timeout(self, ms: int) -> None:
        self.waited_ms.append(ms)

    def title(self) -> str:
        return f"标题 · {self.url}"

    def evaluate(self, js: str) -> Any:
        if self.evaluate_error:
            raise RuntimeError(self.evaluate_error)
        self.evaluated.append(js)
        return {"ran": js}


class FakeContext:
    def __init__(
        self,
        *,
        pages: tuple[FakePage, ...] = (),
        cookies: list[dict[str, Any]] | None = None,
        dead: str = "",
    ) -> None:
        self.pages: list[FakePage] = list(pages)
        self._cookies = cookies if cookies is not None else []
        self.dead = dead
        self.closed = False
        self.probe_urls: list[str] = []
        self.new_pages = 0

    def cookies(self, urls: list[str]) -> list[dict[str, Any]]:
        """`dead` 非空 = 复现"浏览器被关掉之后 context 上的每个调用都抛"。"""
        self.probe_urls.extend(urls)
        if self.dead:
            raise RuntimeError(self.dead)
        return self._cookies

    def new_page(self) -> FakePage:
        self.new_pages += 1
        page = FakePage()
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.closed = True


class FakePlaywright:
    """只记 `launch_persistent_context` 的入参 —— §7.18 的判据就在这些目录字符串里。"""

    def __init__(self, *, contexts: list[FakeContext] | None = None, fail: str = "") -> None:
        self.launches: list[dict[str, Any]] = []
        self._contexts = contexts or []
        self._fail = fail
        self.chromium = self  # V1 的调用形状：`playwright.chromium.launch_persistent_context(...)`

    def launch_persistent_context(self, user_data_dir: str, **options: Any) -> FakeContext:
        self.launches.append({"dir": user_data_dir, "options": options})
        if self._fail:
            raise RuntimeError(self._fail)
        if self._contexts:
            return self._contexts.pop(0)
        return FakeContext(pages=(FakePage(),))

    @property
    def launched_dirs(self) -> list[str]:
        return [entry["dir"] for entry in self.launches]


class ThreadlessWorker(BrowserWorker):
    """真的 handler + 真的自愈判定，只是**不起 Playwright 线程**。

    `submit()` 要求 worker 线程活着（那是给真服务用的守卫），而集成测试要跑的是
    "真 HTTP 请求 → 真 handler → 真 run_job 自愈"这一整段，所以在这里把排队那一步
    换成直接调用。真起线程的那一档在 `real_network` 用例。
    """

    def __init__(self, *, playwright: FakePlaywright, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.playwright = playwright

    def submit(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.fatal:  # 真 submit 的第一道守卫，双份都得有，否则"缺依赖"这条路测不到
            raise RuntimeError(self.fatal)
        status, value = self.run_job(kind, payload, lambda: self._relaunch(self.playwright))
        if status == "error":
            raise RuntimeError(str(value))
        return value if isinstance(value, dict) else {"result": value}


def make_worker(
    tmp_path: Path,
    *,
    page: FakePage | None = None,
    context: FakeContext | None = None,
    headless: bool = False,
    channel: str = "chrome",
    timeout: float = 5.0,
    settle_ms: int = 1500,
    restart_cooldown_seconds: float = 30.0,
) -> BrowserWorker:
    """装配一个**没有线程**的 worker：页面与 context 直接塞进去，只测 handler 行为。"""
    landed = page or FakePage()
    worker = BrowserWorker(
        profile=tmp_path / "profile",
        headless=headless,
        channel=channel,
        timeout=timeout,
        settle_ms=settle_ms,
        restart_cooldown_seconds=restart_cooldown_seconds,
    )
    worker.page = landed
    worker.context = context or FakeContext(pages=(landed,), cookies=COOKIE_SAMPLE)
    return worker
