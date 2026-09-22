"""`CookieManager` 与 Netscape 文件。

**这些文件是有效会话凭证**（V1 §1.1）。所以这一层的判据不是"能不能读"，
而是"会不会把一份看起来能用、其实空壳的文件交出去"。

`looks_empty` 那条是回归看护：2026-09-22 这一行在编辑中被写坏成
`return self.exists and self.size_bytes is not None` —— **仍然返回 bool，
mypy 不报、ruff 不报**，但语义从"空文件"变成"文件存在"。
是冒烟脚本发现"111 字节、两条 cookie 的文件被报成 empty"才抓到的。
**类型检查不会替你读语义。**
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from intelligence_hub_v2.errors import BridgeError, CookieError
from intelligence_hub_v2.infra.cookies import COOKIE_FILE_HEADER, CookieManager
from intelligence_hub_v2.storage.files import FileStorage


@pytest.fixture
def manager(tmp_path: Path) -> CookieManager:
    return CookieManager(FileStorage(tmp_path / "data"))


# ---------------------------------------------------------------------------
# 有没有文件
# ---------------------------------------------------------------------------


def test_exported_path_is_none_when_absent(manager: CookieManager) -> None:
    """返回 None 而不是"不存在的路径"：后者会被原样传进 `--cookies`，
    yt-dlp 报"打不开文件"，看起来像权限问题。"""
    assert manager.exported_path("douyin.com") is None


def test_exported_path_rejects_anything_that_is_not_a_host(manager: CookieManager) -> None:
    with pytest.raises(ValueError, match="域名"):
        manager.exported_path("not a domain")


def test_roundtrip_after_write(manager: CookieManager) -> None:
    manager.write_netscape("douyin.com", [{"name": "a", "value": "b", "domain": "douyin.com"}])
    assert manager.exported_path("douyin.com") is not None


# ---------------------------------------------------------------------------
# Netscape 格式
# ---------------------------------------------------------------------------


def test_header_line_is_first(manager: CookieManager) -> None:
    """少了它，某些解析器会把第一条 cookie 当注释吃掉，报出来的错与"真的没登录"同形。"""
    path = manager.write_netscape(
        "bilibili.com", [{"name": "SESSDATA", "value": "x", "domain": ".bilibili.com"}]
    )
    assert path.read_text(encoding="utf-8").startswith(COOKIE_FILE_HEADER)


def test_seven_tab_fields_in_the_fixed_order(manager: CookieManager) -> None:
    path = manager.write_netscape(
        "douyin.com",
        [
            {
                "name": "sessionid",
                "value": "abc",
                "domain": ".douyin.com",
                "path": "/",
                "secure": True,
                "expires": 1_800_000_000,
            }
        ],
    )
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    fields = lines[1].split("\t")
    assert fields == [
        ".douyin.com",
        "TRUE",
        "/",
        "TRUE",
        "1800000000",
        "sessionid",
        "abc",
    ]


def test_wildcard_flag_follows_the_domain_not_secure(manager: CookieManager) -> None:
    """第 2 列是"域是 `.example.com` 这种通配形式吗"，与 `secure` 不是一回事。"""
    path = manager.write_netscape(
        "douyin.com",
        [
            {"name": "a", "value": "1", "domain": "douyin.com", "secure": True},
            {"name": "b", "value": "2", "domain": ".douyin.com", "secure": False},
        ],
    )
    rows = [line.split("\t") for line in path.read_text(encoding="utf-8").splitlines()[1:]]
    assert [(r[0], r[1], r[3]) for r in rows] == [
        ("douyin.com", "FALSE", "TRUE"),
        (".douyin.com", "TRUE", "FALSE"),
    ]


def test_session_cookie_expires_is_zero_not_minus_one(manager: CookieManager) -> None:
    """Playwright 给会话 cookie 的是 `-1`；Netscape 里的会话是 **0**。

    写 -1 有些解析器当成"早就过期了"直接丢 —— 于是整份 cookie 少一半，还报不出来。
    `True` 也要挡：`bool` 是 `int` 的子类，不挡会写出 `expires=1`（1970 年）。
    """
    path = manager.write_netscape(
        "douyin.com",
        [
            {"name": "sess", "value": "v", "domain": "douyin.com", "expires": -1},
            {"name": "none", "value": "v", "domain": "douyin.com", "expires": None},
            {"name": "bool", "value": "v", "domain": "douyin.com", "expires": True},
        ],
    )
    rows = [line.split("\t") for line in path.read_text(encoding="utf-8").splitlines()[1:]]
    assert [r[4] for r in rows] == ["0", "0", "0"]


def test_a_cookie_value_containing_a_tab_still_parses_as_one_row(
    manager: CookieManager,
) -> None:
    """值里有 tab 会得到**一行八段**，而 Netscape 只认七段。

    这里**不**声明"我们修好了"：桥不会给含 tab 的 cookie 值（RFC 6265 禁止），
    所以这条只是把"真出现时文件长什么样"钉下来，将来出问题能认出是这个。
    """
    path = manager.write_netscape(
        "douyin.com", [{"name": "n", "value": "有\ttab", "domain": "douyin.com"}]
    )
    text = path.read_text(encoding="utf-8")
    assert text.count("\n") == 2  # 表头 + 一条（没被撑成两行）
    row = text.splitlines()[1]
    assert row.split("\t") == ["douyin.com", "FALSE", "/", "FALSE", "0", "n", "有", "tab"]


# ---------------------------------------------------------------------------
# 不许静默产出空壳文件
# ---------------------------------------------------------------------------


def test_write_refuses_an_unusable_list(manager: CookieManager) -> None:
    """写一个只有表头的文件更糟：下一次 `exported_path()` 会 happily 返回它，
    于是整条链路静默退化成匿名下载，而档位差别是**画质**（V1 §7.15）。"""
    with pytest.raises(CookieError, match="没有一条"):
        manager.write_netscape("douyin.com", [])
    with pytest.raises(CookieError, match="没有一条"):
        manager.write_netscape(
            "douyin.com",
            [{"value": "没有名字"}, {"name": "", "value": "x"}],  # type: ignore[dict-item]
        )
    assert manager.exported_path("douyin.com") is None


def test_freshness_does_not_call_a_real_file_empty(manager: CookieManager) -> None:
    """回归看护（见模块 docstring）：两条 cookie 的文件不许被报成空壳。"""
    path = manager.write_netscape(
        "douyin.com",
        [
            {"name": "sessionid", "value": "a" * 32, "domain": ".douyin.com"},
            {"name": "ttwid", "value": "b" * 32, "domain": ".douyin.com"},
        ],
    )
    reading = manager.freshness("douyin.com")
    assert reading.exists is True
    assert reading.size_bytes == path.stat().st_size
    assert reading.size_bytes > 60
    assert reading.looks_empty is False
    assert reading.age_seconds is not None and reading.age_seconds >= 0.0


def test_missing_and_empty_shell_are_two_different_states(manager: CookieManager) -> None:
    """`exists=False` 与 `looks_empty=True` 要分得开：
    前者是"没导出过"，后者是"导出过但没内容"，人要做的事不同。"""
    assert manager.freshness("douyin.com").exists is False
    assert manager.freshness("douyin.com").looks_empty is False

    manager.directory.mkdir(parents=True, exist_ok=True)
    (manager.directory / "douyin.com.txt").write_text(COOKIE_FILE_HEADER, encoding="utf-8")
    reading = manager.freshness("douyin.com")
    assert reading.exists is True
    assert reading.looks_empty is True


# ---------------------------------------------------------------------------
# 从桥刷新
# ---------------------------------------------------------------------------


class _FakeBridge:
    def __init__(self, cookies: list[dict[str, Any]] | None, *, raises: bool = False) -> None:
        self._cookies = cookies or []
        self._raises = raises

    async def cookies(self, domain: str) -> list[dict[str, Any]]:
        if self._raises:
            raise BridgeError("bridge", "store", "桥没起")
        return self._cookies


async def test_refresh_from_bridge_writes_what_it_got(manager: CookieManager) -> None:
    bridge = _FakeBridge([{"name": "sessionid", "value": "abc", "domain": ".douyin.com"}])
    path = await manager.refresh_from_bridge("douyin.com", bridge)  # type: ignore[arg-type]
    assert path == manager.exported_path("douyin.com")
    assert "sessionid" in path.read_text(encoding="utf-8")


async def test_empty_bridge_result_becomes_a_login_prompt(manager: CookieManager) -> None:
    """桥里没这个域 = **还没登录**。要人去做那一步，不许降级成匿名继续跑。"""
    with pytest.raises(CookieError, match="人工登录"):
        await manager.refresh_from_bridge("douyin.com", _FakeBridge([]))  # type: ignore[arg-type]
    assert manager.exported_path("douyin.com") is None


async def test_bridge_failure_is_reported_as_a_cookie_problem_with_the_original_text(
    manager: CookieManager,
) -> None:
    """调用方是在"能不能下载"的语境里问的，所以给 `CookieError`，并把桥的原文带上。"""
    with pytest.raises(CookieError, match="桥没起"):
        await manager.refresh_from_bridge("douyin.com", _FakeBridge(None, raises=True))  # type: ignore[arg-type]


async def test_refresh_overwrites_atomically_and_leaves_no_temp_file(
    manager: CookieManager,
) -> None:
    manager.write_netscape("douyin.com", [{"name": "old", "value": "1", "domain": "douyin.com"}])
    await manager.refresh_from_bridge(  # type: ignore[arg-type]
        "douyin.com", _FakeBridge([{"name": "new", "value": "2", "domain": "douyin.com"}])
    )
    path = manager.exported_path("douyin.com")
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "new" in text and "old" not in text
    assert list(manager.directory.glob("*.tmp")) == []


def test_the_directory_always_comes_from_file_storage(tmp_path: Path) -> None:
    """域名→文件名的规则只有一处（`FileStorage.cookies_path`）。
    自己拼 `cookies_dir / f"{domain}.txt"` 就绕过了"这到底像个域名吗"的校验。"""
    files = FileStorage(tmp_path / "data")
    manager = CookieManager(files)
    assert manager.directory == files.cookies_dir
    written = manager.write_netscape("douyin.com", [{"name": "a", "value": "b", "domain": "d"}])
    # 同一个域，写成 URL 还是主机名都落到同一个文件 —— 校验与归一在 FileStorage 那一处
    assert written == files.cookies_path("https://douyin.com/user/x")
    assert manager.exported_path("https://douyin.com") is not None
