"""CDP 桥服务端（`bridge/server.py`）的 worker 行为测试。

假 Playwright 句柄在 `tests/_bridge_doubles.py`，这里不碰真浏览器。要钉的是几条 V1 教训：

- 导航失败必须**擦页面 + 毒掉下一次求值**（在残留页面上收割 = 拿到上一个博主的作品）。
- "浏览器没了"必须**往外抛**，不能消化成 `ok:false`（否则自愈分支永远走不到，§7.20）。
- 重建必须沿用**同一个 profile 目录**，登录态才跨得过重启（§7.18）。
- 重建有冷却，否则每条请求弹一个 Chrome。
- 只绑回环（AGENTS §1.2），`--allow-non-loopback` 是显式开关。

真 HTTP 往返 + 真服务端在 `tests/integration/test_bridge_health.py`。
"""

from __future__ import annotations

import inspect
import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from tests._bridge_doubles import (
    BROWSER_DEAD,
    COOKIE_SAMPLE,
    FakeContext,
    FakePage,
    FakePlaywright,
    make_worker,
)

from intelligence_hub_v2.bridge import server as server_module
from intelligence_hub_v2.bridge.server import (
    CHROME_ARGS,
    DEFAULT_PORT,
    DEFAULT_WAIT_UNTIL,
    LOOPBACK_HOSTS,
    WAIT_UNTIL_STATES,
    BrowserWorker,
    cast_result,
    clean_url,
    main,
    make_server,
    parse_args,
    resolve_profile,
)
from intelligence_hub_v2.core.config import AppConfig, BridgeSection
from intelligence_hub_v2.infra.cdp_bridge import BridgeClient

# --------------------------------------------------------------------------- #
# /navigate：V1 那三条最贵的行为
# --------------------------------------------------------------------------- #


def test_navigate_returns_the_landed_url_and_records_it(tmp_path: Path) -> None:
    """走 `run_job` 而不是直接调 handler：`last_page_url` 是分发那一层记的。"""
    page = FakePage()
    worker = make_worker(tmp_path, page=page, settle_ms=900)
    status, out = worker.run_job(
        "navigate", {"url": "https://www.douyin.com/user/MS4"}, lambda: False
    )
    assert status == "ok"
    assert out["ok"] is True
    assert out["url"] == "https://www.douyin.com/user/MS4"
    assert out["settle_ms"] == 900
    assert page.waited_ms == [900]  # 等待发生在落地之后，不是之前
    assert worker.last_page_url == out["url"]


def test_a_failed_navigation_blanks_the_page_and_poisons_the_next_evaluate(
    tmp_path: Path,
) -> None:
    """留在上一页 = 下一次求值收割到**上一个博主**的作品，比直接报错危险得多。"""
    page = FakePage(url="https://www.douyin.com/user/A", goto_error="ERR_CONNECTION_CLOSED")
    worker = make_worker(tmp_path, page=page)

    out = worker.handle_navigate({"url": "https://www.douyin.com/user/B"})
    assert out["ok"] is False
    assert "ERR_CONNECTION_CLOSED" in out["error"]
    assert page.url == "about:blank"  # 页面被擦干净了

    poisoned = worker.handle_evaluate({"expression": "() => 1"})
    payload = json.loads(str(poisoned["result"]))
    assert payload["ok"] is False
    assert "导航失败" in payload["error"]
    assert page.evaluated == []  # 毒化期间**没有**在任何页面上跑 JS

    again = worker.handle_evaluate({"expression": "() => 2"})
    assert page.evaluated == ["() => 2"]  # 毒只生效一次，不会永久卡住桥
    assert "result" in again


def test_a_dead_browser_is_raised_not_swallowed(tmp_path: Path) -> None:
    """消化成 `ok:false` 的话 `run_job` 永远看不见该重建（V1 2026-09-21 踩过）。"""
    page = FakePage(url="https://www.douyin.com/user/A", goto_error=BROWSER_DEAD)
    worker = make_worker(tmp_path, page=page)
    with pytest.raises(RuntimeError, match="closed"):
        worker.handle_navigate({"url": "https://www.douyin.com/user/B"})
    assert worker.pending_error == ""  # 没留下"导航失败"的毒，留的是该重建的信号


def test_evaluate_requires_an_expression(tmp_path: Path) -> None:
    worker = make_worker(tmp_path)
    with pytest.raises(ValueError, match="expression"):
        worker.handle_evaluate({"expression": "   "})


def test_evaluate_navigates_first_when_the_payload_carries_a_url(tmp_path: Path) -> None:
    """小红书那条"一次请求导航+求值"的路；导航失败时求值不该发生（V1 同一形状）。"""
    page = FakePage()
    worker = make_worker(tmp_path, page=page)
    out = worker.handle_evaluate({"expression": "() => 1", "url": "https://xhslink.com/x"})
    assert page.visited == ["https://xhslink.com/x#{domcontentloaded}"]
    assert page.evaluated == ["() => 1"]
    assert out["url"] == "https://xhslink.com/x"

    broken = FakePage(goto_error="ERR_ABORTED")
    worker2 = make_worker(tmp_path / "b", page=broken)
    failed = worker2.handle_evaluate({"expression": "() => 1", "url": "https://xhslink.com/y"})
    assert json.loads(str(failed["result"]))["ok"] is False
    assert broken.evaluated == []  # 导航没成的页面不会被收割


def test_settle_ms_on_the_request_overrides_the_default(tmp_path: Path) -> None:
    page = FakePage()
    worker = make_worker(tmp_path, page=page, settle_ms=1500)
    worker.handle_evaluate({"expression": "() => 1", "settle_ms": "250"})
    assert page.waited_ms == [250]
    worker.handle_evaluate({"expression": "() => 2", "settle_ms": "not-a-number"})
    assert page.waited_ms == [250, 1500]  # 乱值退回默认，而不是把整条请求弄崩


# --------------------------------------------------------------------------- #
# wait_until：客户端每趟都发，服务端不能当没看见
# --------------------------------------------------------------------------- #


def test_navigate_honours_wait_until_and_rejects_unknown_values(tmp_path: Path) -> None:
    page = FakePage()
    worker = make_worker(tmp_path, page=page)
    worker.handle_navigate({"url": "https://example.com/", "wait_until": "networkidle"})
    assert page.visited == ["https://example.com/#{networkidle}"]

    worker.handle_navigate({"url": "https://example.org/"})
    assert page.visited[-1] == "https://example.org/#{domcontentloaded}"

    with pytest.raises(ValueError, match="wait_until"):
        worker.handle_navigate({"url": "https://example.com/", "wait_until": "whenever"})


def test_the_clients_default_wait_until_is_the_servers_one() -> None:
    """两边各写一遍默认值就一定会漂：这里直接把客户端的签名拉过来比。"""
    default = inspect.signature(BridgeClient.navigate).parameters["wait_until"].default
    assert default == DEFAULT_WAIT_UNTIL
    assert DEFAULT_WAIT_UNTIL in WAIT_UNTIL_STATES


# --------------------------------------------------------------------------- #
# 自愈（§7.18 + §7.20）
# --------------------------------------------------------------------------- #


def test_run_job_relaunches_once_and_replays_the_request(tmp_path: Path) -> None:
    worker = make_worker(tmp_path)
    seen: list[dict[str, Any]] = []

    def flaky(payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(payload)
        if len(seen) == 1:
            raise RuntimeError(BROWSER_DEAD)
        return {"ok": True, "url": "https://example.com/"}

    worker.handle_flaky = flaky  # type: ignore[method-assign]
    relaunches = 0

    def relaunch() -> bool:
        nonlocal relaunches
        relaunches += 1
        return True

    status, value = worker.run_job("flaky", {"n": 1}, relaunch)
    assert status == "ok"
    assert value["ok"] is True
    assert relaunches == 1 and len(seen) == 2
    assert worker.last_page_url == "https://example.com/"


def test_run_job_reports_the_first_error_when_the_relaunch_fails(tmp_path: Path) -> None:
    """调用方要看到的是作品为什么没采到，而不是"我们的恢复动作失败了"。"""
    worker = make_worker(tmp_path)

    def dead(payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(BROWSER_DEAD)

    worker.handle_dead = dead  # type: ignore[method-assign]
    status, value = worker.run_job("dead", {}, lambda: False)
    assert status == "error"
    assert BROWSER_DEAD in str(value) and "重建" not in str(value)


def test_a_site_error_does_not_trigger_a_relaunch(tmp_path: Path) -> None:
    """只有 Playwright 那句"浏览器没了"才该弹新窗口；网站侧报错重建也救不回来。"""
    worker = make_worker(tmp_path)

    def site_error(payload: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("只接受完整的 http/https 地址")

    worker.handle_site_error = site_error  # type: ignore[method-assign]
    called: list[int] = []
    status, value = worker.run_job("site_error", {}, lambda: called.append(1) or True)
    assert status == "error" and called == []
    assert "http/https" in str(value)


def test_a_relaunch_reuses_the_same_profile_dir(tmp_path: Path) -> None:
    """§7.18：登录态存在 profile 里，所以重建**必须**沿用同一个目录，才不用重新扫码。"""
    worker = make_worker(tmp_path, headless=True)
    playwright = FakePlaywright(contexts=[FakeContext(pages=(FakePage(),))])
    old = worker.context
    assert worker._relaunch(playwright) is True

    assert playwright.launched_dirs == [str(worker.profile)]
    assert Path(str(worker.profile)).is_dir()  # 目录桥自己建，不要人手工栽一棵
    assert old.closed is True  # 换新的之前先关旧的，否则漏一个 Chrome 进程
    assert worker.restarts == 1

    options = playwright.launches[0]["options"]
    assert options["headless"] is True
    for arg in CHROME_ARGS:
        assert arg in options["args"]  # 反自动化标记在重建时也不能丢


def test_relaunch_inside_the_cooldown_window_spawns_no_second_chrome(tmp_path: Path) -> None:
    worker = make_worker(tmp_path, restart_cooldown_seconds=3600.0)
    playwright = FakePlaywright()
    assert worker._relaunch(playwright) is True
    assert worker._relaunch(playwright) is False  # 冷却期内
    assert worker._relaunch(playwright) is False
    assert len(playwright.launches) == 1  # "每条请求弹一个窗口"就是 V1 §7.20 那句


def test_a_failed_relaunch_leaves_the_bridge_usable_next_round(tmp_path: Path) -> None:
    worker = make_worker(tmp_path, restart_cooldown_seconds=0.0)
    broken = FakePlaywright(fail="no chrome here")
    assert worker._relaunch(broken) is False
    assert "no chrome here" in worker.relaunch_error
    assert worker.pending_error == ""  # 重建失败不该继续毒下一次求值
    assert worker._relaunch(FakePlaywright()) is True  # 冷却 0 → 下一轮还能再试


# --------------------------------------------------------------------------- #
# /health 与 /cookies 的载荷形状
# --------------------------------------------------------------------------- #


def test_health_probes_the_browser_with_a_synthetic_url(tmp_path: Path) -> None:
    """探活必须真过一趟浏览器（`page.url` 在浏览器死后仍返回缓存值），
    但**不能**导航到任何站点 —— 会被风控，也会污染当前页。"""
    page = FakePage(url="https://www.douyin.com/user/MS4")
    context = FakeContext(pages=(page,))
    worker = make_worker(tmp_path, page=page, context=context)
    out = worker.handle_health({})
    assert out["ok"] is True
    assert out["page_url"] == page.url
    assert out["channel"] == "chrome"
    assert out["uptime_seconds"] >= 0
    assert context.probe_urls, "没往浏览器发查询 = 探针是假的"
    assert all("bridge-self-check.invalid" in url for url in context.probe_urls)


def test_a_dead_browser_reports_ok_false_and_never_fakes_page_url(tmp_path: Path) -> None:
    page = FakePage(url="https://www.douyin.com/user/A")
    worker = make_worker(tmp_path, page=page, context=FakeContext(pages=(page,), dead=BROWSER_DEAD))
    worker.last_page_url = "https://www.douyin.com/user/A"
    out = worker.handle_health({})
    assert out["ok"] is False
    assert BROWSER_DEAD in out["error"]
    assert out["page_url"] == ""  # 当前页面：不知道
    assert out["last_page_url"] == "https://www.douyin.com/user/A"  # 缓存值换个字段当线索


def test_cookies_returns_the_raw_list_the_v2_client_needs(tmp_path: Path) -> None:
    """V1 只回 `netscape` 文本，而 V2 的渲染器只有一处（`infra/cookies.py`）—— 桥给原始列表。"""
    worker = make_worker(tmp_path)
    out = worker.handle_cookies({"domain": ".douyin.com"})
    assert out["ok"] is True and out["domain"] == "douyin.com"
    assert out["count"] == len(out["cookies"]) == len(COOKIE_SAMPLE)
    assert [c["name"] for c in out["cookies"]] == ["sessionid", "ttwid"]
    assert "netscape" not in out


def test_cookies_without_a_domain_is_an_error_not_an_empty_export(tmp_path: Path) -> None:
    worker = make_worker(tmp_path)
    with pytest.raises(ValueError, match="domain"):
        worker.handle_cookies({"domain": "  .  "})


# --------------------------------------------------------------------------- #
# 起不来 / 不绑出去
# --------------------------------------------------------------------------- #


def test_a_worker_without_playwright_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺依赖的正确行为是如实失败（AGENTS §1.3），不是静默降级成"没登录态也能采"。"""
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    worker = make_worker(tmp_path)
    worker._loop()
    assert "Playwright" in worker.fatal
    with pytest.raises(RuntimeError, match="Playwright"):
        worker.submit("health", {})


def test_submitting_without_a_live_thread_is_an_error(tmp_path: Path) -> None:
    worker = make_worker(tmp_path)
    assert worker.thread.is_alive() is False
    with pytest.raises(RuntimeError, match="线程已经退出"):
        worker.submit("health", {})


def test_jobs_run_serialised_on_the_browser_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`start()` 的 ready 门 + `submit()` 的排队往返：HTTP 线程**从不**直接碰浏览器。

    `_loop` 的函数体需要真 Playwright（那是 provider 的边界，量它在 real_network 那条用例里），
    所以这里只换掉线程体，保留队列、reply、ready 门与错误分支 ——
    验的是"作业在另一条线程上、按提交顺序串行"这一条关系。
    """
    ran_on: list[int] = []

    def fake_loop(self: BrowserWorker) -> None:
        self.ready.set()  # 真 _loop 也是先起浏览器再 set：start() 就是在等这一位
        while True:
            job = self.jobs.get()
            if job is None:
                return
            kind, payload, reply = job
            ran_on.append(threading.get_ident())
            if kind == "boom":
                reply.put(("error", "浏览器没了"))
                continue
            reply.put(("ok", {"ok": True, "kind": kind, "echo": payload}))

    monkeypatch.setattr(BrowserWorker, "_loop", fake_loop)
    worker = make_worker(tmp_path)
    worker.start()  # 不返回就说明 ready 门没通过
    try:
        first = worker.submit("navigate", {"url": "a"})
        second = worker.submit("evaluate", {"js": "b"})
        assert [first["echo"], second["echo"]] == [{"url": "a"}, {"js": "b"}]
        assert ran_on and set(ran_on) != {threading.get_ident()}
        assert len(set(ran_on)) == 1  # 两条作业在同一条浏览器线程上，一条接一条
        with pytest.raises(RuntimeError, match="浏览器没了"):
            worker.submit("boom", {})
    finally:
        worker.shutdown()
        worker.thread.join(timeout=5)
        assert worker.thread.is_alive() is False


def test_the_bind_guard_refuses_a_non_loopback_host_unless_told_otherwise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bound: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        server_module, "ThreadingHTTPServer", lambda addr, handler: bound.append(addr)
    )
    worker = make_worker(tmp_path)

    with pytest.raises(SystemExit, match="只监听回环"):
        make_server(parse_args(["--host", "0.0.0.0"]), worker)
    assert bound == []

    make_server(parse_args(["--host", "0.0.0.0", "--allow-non-loopback"]), worker)
    assert bound == [("0.0.0.0", DEFAULT_PORT)]


def test_the_default_bind_is_loopback_and_the_escape_hatch_is_off() -> None:
    """`--allow-non-loopback` 不进默认配置（AGENTS §1.2）。"""
    args = parse_args([])
    assert args.host in LOOPBACK_HOSTS
    assert args.allow_non_loopback is False


def test_the_server_defaults_agree_with_the_client_config() -> None:
    """配置里连的端口与服务端听的端口各写一遍，早晚会漂成"桥在跑但没人连得上"。"""
    url = BridgeSection().url
    assert url.port == DEFAULT_PORT
    assert str(url.host) == parse_args([]).host


def test_the_default_profile_lives_under_the_configured_data_dir(tmp_path: Path) -> None:
    config = AppConfig()
    config.data.dir = tmp_path / "data"
    profile = resolve_profile(parse_args([]), config)
    assert profile == (tmp_path / "data" / "cdp-bridge-profile").resolve()
    assert not profile.exists()  # 只算路径，建目录是 `_launch_context` 的事

    override = resolve_profile(parse_args(["--profile", str(tmp_path / "elsewhere")]), config)
    assert override == (tmp_path / "elsewhere").resolve()


@pytest.mark.parametrize("bad", ["file:///etc/passwd", "javascript:alert(1)", "关于我", "//x"])
def test_only_http_urls_reach_the_browser(bad: str) -> None:
    """桥在已登录的浏览器里执行任意表达式，不能让外部把它勾到 file:// 上。"""
    with pytest.raises(ValueError, match="http/https"):
        clean_url(bad)


def test_cast_result_only_widens_a_dict_and_nothing_else() -> None:
    assert cast_result({"ok": True}) == {"ok": True}
    assert cast_result("文本") == {"result": "文本"}


def test_shutdown_closes_the_context_and_stops_the_queue(tmp_path: Path) -> None:
    context: FakeContext = FakeContext(pages=(FakePage(),))
    worker = make_worker(tmp_path, context=context)
    worker.shutdown()
    assert context.closed is True
    assert worker.jobs.get_nowait() is None  # 哨兵：线程循环就此退出


# --------------------------------------------------------------------------- #
# main()：三种退出码都要说实话（AGENTS §1.3）
# --------------------------------------------------------------------------- #


def test_a_browser_that_wont_start_is_exit_code_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没 Chrome / 没装 Playwright 时的正确行为是**红着退出**，不是 0 也不是静默重试。"""

    def boom(self: BrowserWorker) -> None:
        raise RuntimeError("浏览器没能在超时前起来")

    monkeypatch.setattr(BrowserWorker, "start", boom)
    monkeypatch.setattr(server_module, "setup_logging", lambda **kwargs: None)
    assert main(["--profile", str(tmp_path / "p")]) == 1


def test_a_taken_port_is_exit_code_one_and_still_shuts_the_worker_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(BrowserWorker, "start", lambda self: None)
    monkeypatch.setattr(server_module, "setup_logging", lambda **kwargs: None)

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise OSError("端口已被占用")

    monkeypatch.setattr(server_module, "ThreadingHTTPServer", refuse)
    worker_shutdowns: list[None] = []
    monkeypatch.setattr(BrowserWorker, "shutdown", lambda self: worker_shutdowns.append(None))
    assert main(["--profile", str(tmp_path / "p")]) == 1
    assert worker_shutdowns, "浏览器已经起来了，绑定失败后不 shutdown 就是漏一个 Chrome"


def test_the_restart_cooldown_really_comes_from_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`cdp_bridge.restart_cooldown_seconds` 在 V2.1 之前是一个没有读者的字段。

    这里不改数值，改的是"配置交出来的那份"，验的是 `main()` 真把它接给了 worker。
    """
    seen: list[float] = []

    class FakeHttpd:
        daemon_threads = False

        def serve_forever(self, poll_interval: float = 0.5) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    monkeypatch.setattr(
        server_module,
        "load_app_config",
        lambda *a, **k: AppConfig(cdp_bridge={"restart_cooldown_seconds": 7}),
    )
    monkeypatch.setattr(
        BrowserWorker, "start", lambda self: seen.append(self.restart_cooldown_seconds)
    )
    monkeypatch.setattr(server_module, "setup_logging", lambda **kwargs: None)
    monkeypatch.setattr(server_module, "ThreadingHTTPServer", lambda addr, handler: FakeHttpd())
    assert main(["--profile", str(tmp_path / "p")]) == 0
    assert seen == [7.0]
