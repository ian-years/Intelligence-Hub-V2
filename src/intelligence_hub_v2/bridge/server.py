"""CDP 桥服务 —— 给采集方提供一个"已登录浏览器 + HTTP 外壳"（provider 侧）。

V1 `cdp_bridge_server.py` 搬进来的那一份，四条契约原样保留：
`GET /health`（**真**往浏览器发一次查询，探不到就 503 + `ok:false`，不回显缓存 URL）、
`POST /navigate`、`POST /evaluate`、`GET /cookies?domain=`，
加上"人把窗口关了就用**同一个 profile** 重建浏览器 + 重建冷却"的自愈（V1 §7.18/§7.20），
以及只绑回环这一条（AGENTS §1.2 —— 它会在你已登录的浏览器里执行任意 JS）。

消费侧是 `infra/cdp_bridge.py` 的 `BridgeClient`。两端对同一份 HTTP 契约的理解由
`tests/integration/test_bridge_health.py` 用**真 HTTP 往返**钉着，不是 mock ——
"客户端多发了一个字段而服务端当没看见"这类漂移只有对着一真的服务才当场红。

与 V1 的四处不同，每处都有原因：

1. `/cookies` 只回 `cookies` 原始列表，不再回 `netscape` 文本。V2 的 Netscape 渲染器在
   `infra/cookies.py::_render_netscape`（带 `-1`→`0` 会话 cookie 那类细节与用例），
   桥里再留一份就是第二处真相；而 `BridgeClient.cookies()` 要的是列表，
   照搬 V1 的形状会让 cookie 那条路恒抛"返回里缺 cookies 列表"。
2. 默认 profile 与重建冷却取自 `config/app.yaml`（`data.dir` → `cdp-bridge-profile/`、
   `cdp_bridge.restart_cooldown_seconds`），不再各处写死一份常量。
3. 日志走 structlog，不再是裸 `print`（常驻服务不能因为 GBK 控制台崩掉）。
4. `Content-Length` 超限回 413 而不是读一半 —— 读掉 2 MB 把剩下的留在 socket 里，
   下一条 keep-alive 请求就从没见过的字节开始。

启动：

    uv run python -X utf8 -m intelligence_hub_v2.bridge.server          # 弹 Chrome，扫码登录一次
    uv run python -X utf8 -m intelligence_hub_v2.bridge.server --headless   # profile 里已有登录态
    make bridge

采集侧连哪个地址由 `cdp_bridge.url` 决定（默认 `http://127.0.0.1:3457`，与这里的 `--port`
默认值一致，那条一致性由 `test_the_server_defaults_agree_with_the_client_config` 钉）。

一条不显然的约束：浏览器 worker 是**单线程 + 一条请求队列**（Playwright 的同步 API 不线程安全），
所以别让桥里的页面去请求桥自己 —— 那条请求排在"等它的 `/navigate`"后面，会互锁到超时
（2026-09-24 真机第一次跑就是这么撞的，形状见集成测试里的 `_static_page`）。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import queue
import threading
import time
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from intelligence_hub_v2.core.config import AppConfig, load_app_config
from intelligence_hub_v2.logging import get_logger, setup_logging
from intelligence_hub_v2.storage.files import FileStorage

__all__ = [
    "BROWSER_DEAD_MARKERS",
    "DEFAULT_PORT",
    "MAX_BODY_BYTES",
    "WAIT_UNTIL_STATES",
    "BridgeHandler",
    "BrowserWorker",
    "browser_dead",
    "clean_url",
    "main",
    "make_server",
    "parse_args",
]

logger = get_logger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3457
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
"""允许绑定的地址。`0.0.0.0` 与局域网 IP 都要 `--allow-non-loopback` 才放行。"""

ALLOWED_SCHEMES = frozenset({"http", "https"})
MAX_BODY_BYTES = 2_000_000
HEALTH_PROBE_URL = "https://bridge-self-check.invalid/"
"""合成域名。`context.cookies(url)` 不发网络，只按 URL 过滤本地 cookie，
但浏览器连接断了它会如实抛 —— 而 `page.url` 不会（那是本地缓存）。"""

# 自动化标记一露出来，抖音/小红书就把页面换成验证滑块，采不到东西。
CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
]

# Playwright 在"浏览器/context/页面已经没了"时的那几句原文。判错了等于给每个
# ValueError 都重建一次浏览器，所以只认这些，宁缺毋滥。
BROWSER_DEAD_MARKERS = (
    "target page, context or browser has been closed",
    "browser has been closed",
    "browser closed",
    "browser context has been shut down",
    "connection closed",
    "target closed",
)

WAIT_UNTIL_STATES = frozenset({"commit", "domcontentloaded", "load", "networkidle"})
DEFAULT_WAIT_UNTIL = "domcontentloaded"


def browser_dead(message: object) -> bool:
    text = str(message or "").lower()
    return any(marker in text for marker in BROWSER_DEAD_MARKERS)


def clean_url(value: object) -> str:
    """只放行 http/https：桥在已登录的浏览器里执行任意表达式，不能让外部把它勾到 file:// 上。"""
    url = str(value or "").strip()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES or not parts.netloc:
        msg = f"只接受完整的 http/https 地址，收到：{url[:120]}"
        raise ValueError(msg)
    return url


class BrowserWorker:
    """浏览器只活在一个线程里：Playwright 的同步 API 不线程安全，所以请求排队进来。"""

    def __init__(
        self,
        *,
        profile: Path,
        headless: bool,
        channel: str,
        timeout: float,
        settle_ms: int,
        restart_cooldown_seconds: float,
    ) -> None:
        self.profile = profile
        self.headless = headless
        self.channel = channel
        self.timeout = timeout
        self.settle_ms = settle_ms
        self.restart_cooldown_seconds = restart_cooldown_seconds
        self.jobs: queue.Queue[Any] = queue.Queue()
        self.ready = threading.Event()
        self.fatal = ""
        self.pending_error = ""
        # Playwright 的对象在本模块里只当"外部句柄"用：桥不许把类型绑到 playwright
        # 上（那是 bridge extra，没装也得能 import 本模块跑测试）。
        self.page: Any = None
        self.context: Any = None
        self.started_at = time.time()
        self.restarts = 0
        self.relaunch_cooldown_until = 0.0
        self.relaunch_error = ""
        self.last_page_url = ""
        self.thread = threading.Thread(target=self._loop, name="cdp-bridge-browser", daemon=True)

    def start(self) -> None:
        self.thread.start()
        if not self.ready.wait(timeout=self.timeout + 30):
            msg = "浏览器没能在超时前起来，看看是不是没有 Chrome：--channel 指一个可用的内核"
            raise RuntimeError(msg)

    def submit(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.fatal:
            raise RuntimeError(self.fatal)
        if not self.thread.is_alive():
            msg = "CDP 桥的浏览器线程已经退出"
            raise RuntimeError(msg)
        reply: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.jobs.put((kind, payload, reply))
        status, value = reply.get(timeout=self.timeout + 30)
        if status == "error":
            raise RuntimeError(str(value))
        return cast_result(value)

    # -- 线程内部 ---------------------------------------------------------- #

    def _launch_context(self, playwright: Any) -> Any:  # noqa: ANN401 - 外部未类型化的句柄
        options: dict[str, Any] = {
            "headless": self.headless,
            "args": list(CHROME_ARGS),
            "viewport": {"width": 1440, "height": 900},
        }
        if self.channel:
            options["channel"] = self.channel
        self.profile.mkdir(parents=True, exist_ok=True)
        return playwright.chromium.launch_persistent_context(str(self.profile), **options)

    def _attach_page(self, context: Any) -> Any:  # noqa: ANN401 - 同上
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(self.timeout * 1000)
        return page

    def _relaunch(self, playwright: Any) -> bool:  # noqa: ANN401 - 同上
        """人在桥拉起的窗口上点关闭之后，用**同一个 profile** 再起一个浏览器。

        登录态存在 profile 目录里，所以重建不需要重新扫码（V1 §7.18，
        看护：`test_a_relaunch_reuses_the_same_profile_dir`）；冷却期挡住"每条请求弹一个窗口"。
        """
        now = time.time()
        if now < self.relaunch_cooldown_until:
            return False
        self.relaunch_cooldown_until = now + self.restart_cooldown_seconds
        old, self.context, self.page = self.context, None, None
        if old is not None:
            # 它已经死了，关不掉是常态。
            with contextlib.suppress(Exception):
                old.close()
        # 被擦掉的那个页面已经不在了，别让它的导航失败继续毒下一次求值。
        self.pending_error = ""
        try:
            self.context = self._launch_context(playwright)
            self.page = self._attach_page(self.context)
        except Exception as exc:  # 重建失败只是这轮采不到，下一轮还能再试
            self.relaunch_error = str(exc) or type(exc).__name__
            logger.exception("bridge.relaunch_failed", error=self.relaunch_error)
            return False
        self.restarts += 1
        self.relaunch_error = ""
        logger.info("bridge.relaunched", restarts=self.restarts, profile=str(self.profile))
        return True

    def _record_url(self, result: dict[str, Any]) -> dict[str, Any]:
        if result.get("ok") and result.get("url"):
            self.last_page_url = str(result["url"])
        return result

    def run_job(
        self,
        kind: str,
        payload: dict[str, Any],
        relaunch: Callable[[], bool],
    ) -> tuple[str, Any]:
        """跑一条请求；判定为"浏览器没了"就重建后重试一次，其它错误原样交出去。

        重建失败时交出去的是**第一条**错误，不是"重建失败"—— 调用方要看到的是作品
        为什么没采到，而不是我们的恢复动作。
        """
        handler: Callable[[dict[str, Any]], dict[str, Any]] = getattr(self, f"handle_{kind}")
        try:
            return "ok", self._record_url(handler(payload))
        except Exception as exc:  # noqa: BLE001 - 单条请求失败不能带走整个桥
            first = str(exc) or type(exc).__name__
        if not browser_dead(first) or not relaunch():
            return "error", first
        try:
            return "ok", self._record_url(handler(payload))
        except Exception as retry_exc:  # noqa: BLE001
            return "error", str(retry_exc) or type(retry_exc).__name__

    def _loop(self) -> None:
        try:
            # 延迟导入：`bridge` 是可选 extra，没装也得能 import 本模块（跑测试、看 --help）。
            from playwright.sync_api import sync_playwright  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001 - 缺依赖对用户就是"桥起不来"
            msg = f"未安装 Playwright（uv sync --extra bridge）：{exc}"
            self.fatal = msg
            self.ready.set()
            return
        try:
            with sync_playwright() as playwright:
                self.context = self._launch_context(playwright)
                self.page = self._attach_page(self.context)
                self.ready.set()
                while True:
                    job = self.jobs.get()
                    if job is None:
                        break
                    kind, payload, reply = job
                    reply.put(self.run_job(kind, payload, lambda: self._relaunch(playwright)))
        except Exception as exc:  # noqa: BLE001 - 浏览器起不来/崩了都是终态
            self.fatal = f"CDP 桥异常退出：{exc}"
            self.ready.set()

    def _goto(self, url: str, wait_until: str) -> dict[str, Any]:
        response = self.page.goto(url, wait_until=wait_until, timeout=self.timeout * 1000)
        self.page.wait_for_timeout(self.settle_ms)
        status = getattr(response, "status", None)
        return {"ok": True, "url": self.page.url, "title": self.page.title(), "http_status": status}

    def handle_navigate(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = clean_url(payload.get("url"))
        wait_until = read_wait_until(payload)
        self.pending_error = ""
        try:
            result = self._goto(url, wait_until)
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            if browser_dead(message):
                # 浏览器整个没了就往外抛：这里既没有"上一个博主的残留页面"可收割，
                # 把它消化成 ok:False 反而让 run_job 看不见该重建（2026-09-21 踩过）。
                raise
            # 留在上一页 = 下一次求值会 harvest 到上一个博主的作品，先把页面擦干净。
            self.pending_error = message
            with contextlib.suppress(Exception):  # 擦不干净也已经有 pending_error 兜着
                self.page.goto("about:blank", timeout=10_000)
            return {"ok": False, "error": self.pending_error, "url": url}
        result["settle_ms"] = self.settle_ms
        result["wait_until"] = wait_until
        return result

    def handle_evaluate(self, payload: dict[str, Any]) -> dict[str, Any]:
        expression = str(payload.get("expression") or "").strip()
        if not expression:
            msg = "/evaluate 需要 expression"
            raise ValueError(msg)
        if payload.get("url"):
            navigated = self.handle_navigate(payload)
            if not navigated.get("ok"):
                return _navigation_failed(navigated.get("error"))
        if self.pending_error:
            error, self.pending_error = self.pending_error, ""
            return _navigation_failed(error)
        settle_ms = _settle_ms_of(payload, default=self.settle_ms)
        if settle_ms > 0:
            self.page.wait_for_timeout(settle_ms)
        # 采集侧传的都是 async 箭头函数本体，Playwright 认得出来并自动调用。
        value = self.page.evaluate(expression)
        return {"result": value, "url": self.page.url}

    def handle_cookies(self, payload: dict[str, Any]) -> dict[str, Any]:
        """按域名取 cookie，交给 `CookieManager.write_netscape()` 落盘（V1 §7.3 那条路）。"""
        domain = str(payload.get("domain") or "").strip().lstrip(".")
        if not domain:
            msg = "/cookies 需要 domain"
            raise ValueError(msg)
        items = self.context.cookies([f"https://{domain}/"])
        cookies = [item for item in items if isinstance(item, dict)]
        return {"ok": True, "count": len(cookies), "domain": domain, "cookies": cookies}

    def handle_health(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload  # 无请求体；签名要合 run_job 按 handle_<kind> 分发的形状。
        dead = self.probe()
        page_url = ""
        title = ""
        cached_url = ""
        try:
            cached_url = self.page.url if self.page else ""
        except Exception:  # noqa: BLE001
            cached_url = ""
        if not dead:
            page_url = cached_url
            try:
                title = self.page.title() if self.page else ""
            except Exception:  # noqa: BLE001 - 页面正在跳转时读不到很正常，活着由 probe 说了算
                title = ""
        info: dict[str, Any] = {
            "ok": not dead,
            "engine": "playwright",
            "channel": self.channel or "chromium",
            "headless": self.headless,
            "profile": str(self.profile),
            "page_url": page_url,
            # 缓存值只在死的时候作为线索出现，并且换个字段名，别冒充当前页面
            "last_page_url": self.last_page_url or cached_url,
            "title": title,
            "restarts": self.restarts,
            "relaunch_cooldown_seconds": self.restart_cooldown_seconds,
            "uptime_seconds": round(time.time() - self.started_at, 1),
        }
        if dead:
            info["error"] = dead
        return info

    def probe(self) -> str:
        """真过一趟浏览器：死了就返回原因，空串表示活着。

        为什么不用 ``page.url`` / ``page.title()``：浏览器被关之后 ``page.url`` 仍返回
        **本地缓存**（看起来完全健康），``title()`` 抛错又常常只是页面在跳转，两者都不能当判据。
        """
        if self.context is None:
            return "浏览器还没起来"
        try:
            self.context.cookies([HEALTH_PROBE_URL])
        except Exception as exc:  # noqa: BLE001 - 探活失败本身就是结论，不该把桥带崩
            return str(exc) or type(exc).__name__
        return ""

    def shutdown(self) -> None:
        self.jobs.put(None)
        if self.context:
            # 收尾失败不该盖住真正的退出原因。
            with contextlib.suppress(Exception):
                self.context.close()


def cast_result(value: object) -> dict[str, Any]:
    """worker 的四个 handler 都回 dict，`queue` 的载荷类型只能是 `Any`，这里收成 dict。"""
    return value if isinstance(value, dict) else {"result": value}


def read_wait_until(payload: dict[str, Any]) -> str:
    """`BridgeClient.navigate()` 每趟都带 `wait_until`，服务端不能当没看见。

    不认识的取值直接抛，而不是默默降级成默认值：静默降级会让调用方以为
    自己要到的是 `networkidle`，而页面其实只等到 `domcontentloaded`。
    """
    raw = str(payload.get("wait_until") or DEFAULT_WAIT_UNTIL).strip()
    if raw not in WAIT_UNTIL_STATES:
        msg = f"wait_until 只认 {sorted(WAIT_UNTIL_STATES)}，收到 {raw!r}"
        raise ValueError(msg)
    return raw


def _settle_ms_of(payload: dict[str, Any], *, default: int) -> int:
    raw = payload.get("settle_ms")
    if raw is None:
        return default
    try:
        parsed = int(str(raw))
    except (TypeError, ValueError):
        # 乱值退回默认，而不是把整条请求弄崩：求值本身跟 settle_ms 没关系。
        return default
    return parsed


def _navigation_failed(error: object) -> dict[str, Any]:
    """导航失败的**求值结果**：包成 JSON 字符串，页面侧脚本 `JSON.parse` 之后看到的形状不变。"""
    body = json.dumps({"ok": False, "error": f"导航失败：{error}"}, ensure_ascii=False)
    return {"result": body}


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "IntelStationCDPBridge/1.0"
    worker: BrowserWorker  # 由 make_server 挂上

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # BaseHTTPRequestHandler 的命名，不是我们的
        parts = urllib.parse.urlsplit(self.path)
        route = parts.path.rstrip("/")
        try:
            if route == "/health":
                payload = self.worker.submit("health", {})
                # 探不到浏览器就给 503：回 200 就等于让看板在桥已经废了的时候继续报绿
                # （V1 §7.20，语义由 BridgeClient.health() 分 reachable / browser_ok 两位）。
                self._json(200 if payload.get("ok", True) else 503, payload)
                return
            if route == "/cookies":
                query = urllib.parse.parse_qs(parts.query)
                payload = self.worker.submit(
                    "cookies", {"domain": (query.get("domain") or [""])[0]}
                )
                self._json(200, payload)
                return
        except Exception as exc:  # noqa: BLE001 - 健康检查要如实报红
            self._json(503, {"ok": False, "error": str(exc)})
            return
        self._json(404, {"ok": False, "error": "只有 /health 和 /cookies"})

    def do_POST(self) -> None:  # BaseHTTPRequestHandler 的命名，不是我们的
        kind = urllib.parse.urlsplit(self.path).path.rstrip("/").lstrip("/")
        if kind not in ("navigate", "evaluate"):
            self._json(404, {"ok": False, "error": "只有 /navigate 和 /evaluate"})
            return
        try:
            declared = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json(400, {"ok": False, "error": "Content-Length 不是整数"})
            return
        if declared > MAX_BODY_BYTES:
            # 读一半就把剩下的留在 socket 里，下一条 keep-alive 请求会从未见过的字节开始。
            self._json(
                413, {"ok": False, "error": f"请求体 {declared} 字节，上限 {MAX_BODY_BYTES}"}
            )
            return
        try:
            payload = json.loads(self.rfile.read(declared).decode("utf-8")) if declared else {}
        except (ValueError, UnicodeDecodeError) as exc:
            self._json(400, {"ok": False, "error": f"请求体不是合法 JSON：{exc}"})
            return
        if not isinstance(payload, dict):
            # JSON 数组/字符串都合法，但 handler 只吃对象 —— 那不是 TypeError 也不是坏 JSON。
            self._json(400, {"ok": False, "error": "请求体得是 JSON 对象"})
            return
        # JSON 对象的键按定义是字符串，mypy 从 `Any` 收窄只到 `dict[Any, Any]`。
        body: dict[str, Any] = payload
        try:
            self._json(200, self.worker.submit(kind, body))
        except Exception as exc:  # noqa: BLE001 - 调用方按结构化失败记原因
            detail = str(exc)
            self._json(
                200,
                {
                    "ok": False,
                    "error": detail,
                    "result": json.dumps({"ok": False, "error": detail}, ensure_ascii=False),
                },
            )

    def log_message(self, fmt: str, *args: object) -> None:
        logger.info("bridge.request", line=f"{self.address_string()} {fmt % args}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="本机 CDP 桥（Playwright + Chrome 持久化 profile）"
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="监听地址，默认只绑回环")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口，默认 3457")
    parser.add_argument(
        "--profile",
        default="",
        help="持久化用户数据目录（登录态存这里），默认 <config data.dir>/cdp-bridge-profile",
    )
    parser.add_argument(
        "--channel",
        default="chrome",
        help="浏览器内核：chrome / msedge / chromium…（置空用 Playwright 自带 chromium）",
    )
    parser.add_argument(
        "--headless", action="store_true", help="无头模式（登录态已有再用，反爬更容易挡无头）"
    )
    parser.add_argument("--timeout", type=float, default=45.0, help="单次导航/求值超时（秒）")
    parser.add_argument(
        "--settle-ms", type=int, default=1500, help="页面 load 之后额外等待，留给前端渲染"
    )
    parser.add_argument(
        "--open", dest="open_url", default="", help="启动后先打开这个地址，方便扫码登录"
    )
    parser.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="明知故犯：允许绑到非回环地址（等于把已登录浏览器开放给局域网）",
    )
    return parser.parse_args(argv)


def resolve_profile(args: argparse.Namespace, config: AppConfig) -> Path:
    """`--profile` 没给就从配置算：`data.dir` 下那个 `cdp-bridge-profile`。

    走 `FileStorage` 而不是在这里拼字符串：那个目录名在 V2 只该有一处定义
    （V1 §7.11 那一族"同一个东西三个叫法"）。
    """
    raw = str(args.profile or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return FileStorage.from_config(config).bridge_profile_dir.resolve()


def make_server(args: argparse.Namespace, worker: BrowserWorker) -> ThreadingHTTPServer:
    if not args.allow_non_loopback and args.host not in LOOPBACK_HOSTS:
        msg = (
            f"拒绝绑定 {args.host}：这个桥会在已登录浏览器里执行任意 JS，只监听回环。"
            f"确实要开放请加 --allow-non-loopback"
        )
        raise SystemExit(msg)
    handler = type("BoundBridgeHandler", (BridgeHandler,), {"worker": worker})
    return ThreadingHTTPServer((args.host, args.port), handler)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_app_config()
    setup_logging(level=config.logging.level, fmt=config.logging.format)
    worker = BrowserWorker(
        profile=resolve_profile(args, config),
        headless=bool(args.headless),
        channel=str(args.channel or "").strip(),
        timeout=max(1.0, float(args.timeout)),
        settle_ms=max(0, int(args.settle_ms)),
        restart_cooldown_seconds=float(config.cdp_bridge.restart_cooldown_seconds),
    )
    try:
        worker.start()
    except (RuntimeError, OSError) as exc:
        logger.exception("bridge.start_failed", error=str(exc), channel=args.channel)
        return 1
    try:
        httpd = make_server(args, worker)
    except OSError as exc:
        worker.shutdown()
        logger.exception("bridge.port_unavailable", host=args.host, port=args.port, error=str(exc))
        return 1
    httpd.daemon_threads = True
    logger.info(
        "bridge.listening",
        listening=f"http://{args.host}:{args.port}",
        profile=str(worker.profile),
        channel=worker.channel or "chromium",
        headless=worker.headless,
    )
    if args.open_url:
        worker.submit("navigate", {"url": args.open_url})
    try:
        httpd.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        logger.info("bridge.stopped")
    finally:
        worker.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
