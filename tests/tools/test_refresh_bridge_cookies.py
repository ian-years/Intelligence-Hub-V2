"""`tools/refresh_bridge_cookies.py` 的用例。

两条路分开验：

- `refresh_domains()` 这一档用 `httpx.MockTransport` 替桥答话 —— 要钉的是
  "空清单不落文件""单域失败不炸整跑""0600 在 replace 之前"这几条形状。
- `main()` 这一档起一个**真 HTTP 服务端**（复用 T0.1 的 `BridgeHandler` + 假浏览器句柄），
  走完整的 CLI → asyncio → httpx → 桥 → Netscape 落盘。工具的验收写的是"从桥导出"，
  那一条 mock 顶不了。
"""

from __future__ import annotations

import json
import re
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOLS = _REPO_ROOT / "tools"
for _entry in (str(_REPO_ROOT), str(_TOOLS)):
    if _entry not in sys.path:  # tools/ 不是包，测试按脚本入口那样把它接进来
        sys.path.insert(0, _entry)

from tests._bridge_doubles import (  # noqa: E402
    COOKIE_SAMPLE,
    FakeContext,
    FakePage,
    FakePlaywright,
    ThreadlessWorker,
)

from intelligence_hub_v2.bridge.server import BridgeHandler  # noqa: E402
from intelligence_hub_v2.infra.cdp_bridge import BridgeClient  # noqa: E402
from intelligence_hub_v2.infra.cookies import COOKIE_FILE_HEADER, CookieManager  # noqa: E402
from intelligence_hub_v2.platforms.bilibili.urls import BILI_COOKIE_DOMAIN  # noqa: E402
from intelligence_hub_v2.platforms.douyin.media import DOUYIN_COOKIE_DOMAIN
from intelligence_hub_v2.platforms.xiaohongshu.media import XHS_COOKIE_DOMAIN  # noqa: E402
from intelligence_hub_v2.storage.files import FileStorage  # noqa: E402
from refresh_bridge_cookies import (  # noqa: E402 - 上面那行 sys.path 是这条路必需的形状
    DEFAULT_DOMAINS,
    count_cookie_lines,
    domains_from,
    main,
    parse_args,
    refresh_domains,
)

_DOMAIN_CONST = re.compile(r"^\w*_COOKIE_DOMAIN\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE)


def _bridge_client(handler: Any) -> BridgeClient:
    return BridgeClient(
        "http://127.0.0.1:3457", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


def _rows(domain: str) -> list[dict[str, Any]]:
    """同一批 cookie，换到指定域名下（带前导点 = 含子域，与真浏览器一致）。"""
    return [{**row, "domain": f".{domain}"} for row in COOKIE_SAMPLE]


def _ok(domain: str, rows: list[dict[str, Any]] | None = None) -> Any:
    """`domain` = 桥**剥掉前导点之后**看到的那个值（`BridgeClient` 不剥，服务端才剥）。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/cookies"
        assert request.url.params["domain"].lstrip(".") == domain
        return httpx.Response(
            200,
            json={
                "ok": True,
                "domain": domain,
                "cookies": _rows(domain) if rows is None else rows,
            },
        )

    return handler


# --------------------------------------------------------------------------- #
# 默认清单与适配器读的是同一个拼写
# --------------------------------------------------------------------------- #


def test_the_default_domains_are_the_platforms_own_constants() -> None:
    """工具不许自己拼域名字符串：它导出的文件名必须正是适配器去找的那个。

    判据跟着**平台**走而不是写死一份名单：从 `platforms/` 里扫 `*_COOKIE_DOMAIN` 常量，
    谁实现了谁就得进默认清单（T2.1 加小红书适配器时这条会自动开始要求它）。
    """
    declared = {
        match
        for path in (_REPO_ROOT / "src" / "intelligence_hub_v2" / "platforms").rglob("*.py")
        for match in _DOMAIN_CONST.findall(path.read_text(encoding="utf-8"))
    }
    assert declared, "一个 `*_COOKIE_DOMAIN` 都没扫到 —— 那是这条判据坏了，不是工具坏了"
    assert (
        set(DEFAULT_DOMAINS)
        == declared
        == {
            DOUYIN_COOKIE_DOMAIN,
            BILI_COOKIE_DOMAIN,
            XHS_COOKIE_DOMAIN,
        }
    )


def test_the_default_domain_files_are_what_the_ladder_looks_for(tmp_path: Path) -> None:
    files = FileStorage(tmp_path / "data")
    assert {files.cookies_path(domain).name for domain in DEFAULT_DOMAINS} == {
        "douyin.com.txt",
        "bilibili.com.txt",
        "xiaohongshu.com.txt",
    }


def test_explicit_domains_win_and_are_deduplicated_in_order() -> None:
    args = parse_args(["--domain", "bilibili.com", "--domain", "bilibili.com", "--domain", "  "])
    assert domains_from(args) == ["bilibili.com"]
    assert domains_from(parse_args([])) == list(DEFAULT_DOMAINS)


# --------------------------------------------------------------------------- #
# 成功路径：桥 → Netscape 文件
# --------------------------------------------------------------------------- #


async def test_a_live_bridge_writes_one_file_per_domain(tmp_path: Path) -> None:
    asked: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        domain = request.url.params["domain"]
        asked.append(domain)
        return httpx.Response(200, json={"ok": True, "domain": domain, "cookies": _rows(domain)})

    manager = CookieManager(FileStorage(tmp_path / "data"))
    result = await refresh_domains(["douyin.com", "bilibili.com"], _bridge_client(handler), manager)

    assert asked == ["douyin.com", "bilibili.com"]
    assert result["failed"] == {} and set(result["written"]) == {"douyin.com", "bilibili.com"}
    bili = (tmp_path / "data" / "cookies" / "bilibili.com.txt").read_text(encoding="utf-8")
    assert bili.startswith(COOKIE_FILE_HEADER)
    assert ".bilibili.com" in bili and ".douyin.com" not in bili, (
        "两个域名的内容串了：文件名对了但内容是别人的会话"
    )


async def test_the_reported_count_is_counted_on_disk_not_taken_from_the_bridge(
    tmp_path: Path,
) -> None:
    """`written[...]["cookies"]` 数的是盘上的行，不是桥报的 `count`。

    拿别人的数字当自己的报告，正是"看板绿着而盘上是空的"那种形状。
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "count": 99, "cookies": _rows("douyin.com")})

    manager = CookieManager(FileStorage(tmp_path / "data"))
    result = await refresh_domains(["douyin.com"], _bridge_client(handler), manager)
    path = Path(result["written"]["douyin.com"]["path"])
    assert result["written"]["douyin.com"]["cookies"] == count_cookie_lines(path) == 2
    assert result["written"]["douyin.com"]["size_bytes"] == path.stat().st_size


# --------------------------------------------------------------------------- #
# 失败：没登录 / 桥没起 / 名字不像域名
# --------------------------------------------------------------------------- #


async def test_a_domain_the_bridge_has_never_seen_is_not_written_as_an_empty_file(
    tmp_path: Path,
) -> None:
    """只有表头的 cookie 文件比"没有文件"更阴：yt-dlp 不报错，只是匿名（V1 §7.15）。"""
    manager = CookieManager(FileStorage(tmp_path / "data"))
    result = await refresh_domains(["douyin.com"], _bridge_client(_ok("douyin.com", [])), manager)

    assert result["written"] == {}
    assert "登录" in result["failed"]["douyin.com"]
    assert not (tmp_path / "data" / "cookies" / "douyin.com.txt").exists()


async def test_a_bridge_that_is_down_fails_per_domain_with_the_original_text(
    tmp_path: Path,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    manager = CookieManager(FileStorage(tmp_path / "data"))
    result = await refresh_domains(["douyin.com", "bilibili.com"], _bridge_client(handler), manager)

    assert result["written"] == {} and len(result["failed"]) == 2
    # 原文里要留得下"桥起了吗"这一问：那是用户唯一看得见的线索。
    assert "桥起了吗" in result["failed"]["douyin.com"]


async def test_a_name_that_is_not_a_domain_is_a_failure_not_a_second_file(tmp_path: Path) -> None:
    """.douyin.com 会被 `cookies_path()` 拒掉；放过它就会多出第二个文件名（§7.11）。"""
    manager = CookieManager(FileStorage(tmp_path / "data"))
    result = await refresh_domains([".douyin.com"], _bridge_client(_ok("douyin.com")), manager)

    assert result["written"] == {} and "ValueError" in result["failed"][".douyin.com"]
    assert not (tmp_path / "data" / "cookies").exists() or not any(
        (tmp_path / "data" / "cookies").iterdir()
    )


async def test_one_bad_name_does_not_stop_the_good_ones(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        domain = request.url.params["domain"]
        return httpx.Response(200, json={"ok": True, "domain": domain, "cookies": _rows(domain)})

    manager = CookieManager(FileStorage(tmp_path / "data"))
    result = await refresh_domains(
        ["douyin.com", "not a domain", "bilibili.com"], _bridge_client(handler), manager
    )
    assert set(result["written"]) == {"douyin.com", "bilibili.com"}
    assert list(result["failed"]) == ["not a domain"]


# --------------------------------------------------------------------------- #
# 权限：凭证文件不该有一瞬间是 0o644
# --------------------------------------------------------------------------- #


async def test_the_mode_is_tightened_before_the_file_reaches_its_final_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """判据是**顺序**：`chmod 0600` 必须在 `replace()` 之前。

    Windows 上 POSIX 位只是建议性的，所以这里不查 `st_mode`（两个平台答案不同），
    查的是写入者有没有在换名之前收紧。
    """
    events: list[str] = []
    real_chmod = Path.chmod
    real_replace = Path.replace

    def chmod(self: Path, mode: int, **kwargs: Any) -> None:
        events.append(f"chmod:{oct(mode)}")
        real_chmod(self, mode, **kwargs)

    def replace(self: Path, target: Any) -> Any:
        events.append(f"replace:{Path(target).name}")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "chmod", chmod)
    monkeypatch.setattr(Path, "replace", replace)

    manager = CookieManager(FileStorage(tmp_path / "data"))
    await refresh_domains(["douyin.com"], _bridge_client(_ok("douyin.com")), manager)

    assert events == ["chmod:0o600", "replace:douyin.com.txt"], (
        f"顺序不对（先 replace 再 chmod 等于让凭证裸奔一瞬间）：{events}"
    )


# --------------------------------------------------------------------------- #
# CLI：真 HTTP 上的验收
# --------------------------------------------------------------------------- #


@pytest.fixture
def fake_bridge(tmp_path: Path) -> Any:
    """真 `BridgeHandler` + 假浏览器：`/cookies` 交出的就是 T0.1 那份形状。"""
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
    worker.context = FakeContext(pages=(page,), cookies=_rows("douyin.com"))
    handler = type("BoundCookieHandler", (BridgeHandler,), {"worker": worker})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _payload(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    """工具交出的那行 JSON = stdout 的**最后一行**。

    假桥与工具在同一个进程里，`BridgeHandler` 的 structlog 行会先进 stdout；
    真跑（`make bridge-cookies`）时桥在另一个进程，stdout 只有这一行。
    """
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines, "工具什么都没打"
    parsed = json.loads(lines[-1])
    assert isinstance(parsed, dict)
    return parsed


def test_main_writes_the_file_and_says_so_on_stdout(
    fake_bridge: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_dir = tmp_path / "data"
    code = main(["--bridge-url", fake_bridge, "--domain", "douyin.com", "--data-dir", str(out_dir)])
    payload = _payload(capsys)

    assert code == 0
    assert payload["ok"] is True and payload["failed"] == {}
    path = Path(payload["written"]["douyin.com"]["path"])
    assert path.parent == out_dir / "cookies"
    assert path.read_text(encoding="utf-8").startswith(COOKIE_FILE_HEADER)
    assert payload["written"]["douyin.com"]["cookies"] == count_cookie_lines(path) == 2


def test_main_exits_one_when_the_bridge_has_no_cookie_for_that_domain(
    fake_bridge: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """桥在、可那个域没登录过 → 红着退出，且不落文件（不是"写个空文件算成功"）。"""
    code = main(
        ["--bridge-url", fake_bridge, "--domain", "bilibili.com", "--data-dir", str(tmp_path)]
    )
    payload = _payload(capsys)
    assert code == 1
    assert payload["ok"] is False and payload["written"] == {}
    assert "bilibili.com" in payload["failed"]
    assert not (tmp_path / "cookies" / "bilibili.com.txt").exists()


def test_main_refuses_a_bridge_url_off_loopback(tmp_path: Path) -> None:
    """AGENTS §1.2 在客户端这一侧的闸门：命令行把桥指到局域网就该拒。"""
    with pytest.raises(ValueError, match="回环"):
        main(["--bridge-url", "http://192.168.1.7:3457", "--data-dir", str(tmp_path)])
